import sys
import os

# 开发态：PyQt5 / keyboard 解包在 libs/，不依赖启动器设置 PYTHONPATH，
# 必须放在 import keyboard 之前。冻结成 exe 后依赖已打进包内，跳过。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs"))

import requests
import json
import re
import posixpath
import queue
import zipfile
import threading
import time
import keyboard
import ctypes
import traceback
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import unquote
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QMenu,
                             QAction, QDialog, QFormLayout, QLineEdit, QSlider,
                             QSpinBox, QDoubleSpinBox, QPushButton, QSystemTrayIcon, QStyle,
                             QColorDialog, QCheckBox, QHBoxLayout,
                             QFrame, QTextEdit, QShortcut, QListWidget,
                             QListWidgetItem, QLabel, QFontComboBox, QSizePolicy, QFileDialog,
                             QInputDialog, QMessageBox, QToolTip, QTabWidget, QScrollArea,
                             QStackedWidget, QGridLayout)
from PyQt5.QtCore import (Qt, QPoint, QRect, QPointF, QSize, pyqtSignal, QObject,
                          QThread, QTimer, QEvent, QUrl)
from PyQt5.QtGui import (QFont, QColor, QCursor, QKeySequence, QPainter, QPen,
                         QFontMetrics, QTextCursor, QTextBlockFormat, QWheelEvent,
                         QPixmap, QIcon, QPainterPath, QTextCharFormat, QPalette)

# 隐形背诵：AnkiConnect 客户端。缺文件也不能让整个阅读器起不来，所以单独兜一层。
try:
    from anki_client import (AnkiClient, AnkiError, AnkiUnavailable, AnkiNotReviewing,
                             AnkiBusy, AnkiNeedsKey, AnkiCommandError, INSTALL_HINT)
    ANKI_CLIENT_OK = True
except Exception as _anki_import_err:      # pragma: no cover
    ANKI_CLIENT_OK = False
    INSTALL_HINT = "隐形背诵需要 anki_client.py（本目录下），当前加载失败。"
    class AnkiError(Exception): pass
    class AnkiUnavailable(AnkiError): pass
    class AnkiNotReviewing(AnkiError): pass
    class AnkiBusy(AnkiError): pass
    class AnkiNeedsKey(AnkiError): pass
    class AnkiCommandError(AnkiError): pass
    AnkiClient = None

# 社区统一流：数据模型 / 来源适配器 / HTML 渲染（纯标准库）。缺文件也不该让阅读器起不来。
try:
    import feed as feed_mod
    import feed_net
    FEED_OK = True
    FEED_IMPORT_HINT = ""
except Exception as _feed_import_err:      # pragma: no cover
    FEED_OK = False
    feed_mod = None
    feed_net = None
    FEED_IMPORT_HINT = f"社区统一流需要 feed.py（本目录下），当前加载失败：{_feed_import_err}"

# 微信读书书架：只到"读到哪了"（正文不做，原因写在 weread.py 里）。同样缺文件也不该起不来。
try:
    import weread as weread_mod
    WEREAD_OK = True
except Exception as _weread_import_err:     # pragma: no cover
    weread_mod = None
    WEREAD_OK = False
    WEREAD_IMPORT_HINT = f"微信读书书架需要 weread.py（本目录下），当前加载失败：{_weread_import_err}"

# 启用高分屏支持
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

def _app_dir():
    """可写目录：配置、标注、缓存、日志都放在这里。

    源码运行时是本文件所在目录；冻结成 exe 后是 exe 所在目录——PyInstaller
    单文件模式的 __file__ 指向临时解包目录，重启即消失，不能用来存数据。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _resource_dir():
    """只读资源目录：随程序分发的示例数据等。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", _app_dir())
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
RES_DIR = _resource_dir()

CONFIG_FILE = os.path.join(APP_DIR, "config.json")

# 行距（字号行高的百分比）、段间距（同样以行高为基准）、页边距（逻辑像素）
LINE_SPACING_MIN = 80
LINE_SPACING_MAX = 250
LINE_SPACING_DEFAULT = 100
PARA_SPACING_MIN = 0
PARA_SPACING_MAX = 200
# 页边距的单位是"字"（1 字 = 一个全角空格宽），随字号字体等比变化：
# 0~3 字 覆盖原来的 0~60px（14pt 新宋体下 1 字 ≈ 19px）
PAGE_MARGIN_MIN = 0.0
PAGE_MARGIN_MAX = 3.0
PAGE_MARGIN_DEFAULT = 1.0
# 首行缩进：1 字符 = 一个全角空格的宽度，所以换字号字体会跟着变
FIRST_LINE_INDENT_MIN = 0.0
FIRST_LINE_INDENT_MAX = 4.0
FIRST_LINE_INDENT_DEFAULT = 2.0
# 翻页时保留的上一页行数。翻一整页仍留几行上文，接续处才不用回头找
PAGE_OVERLAP_LINES = 2
PAGE_OVERLAP_MAX = 5
# 每次滚动的行数：上下箭头与鼠标滚轮共用同一个值（一格 = 这么多行）
ARROW_SCROLL_LINES = 3
ARROW_SCROLL_MAX = 20


def clamp_config_int(config, key, default, low, high):
    """配置里的整数：非法值回落默认，越界值夹到范围内。"""
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def clamp_config_number(config, key, default, low, high):
    """配置里的数值（可为小数）：非法值回落默认，越界值夹到范围内。"""
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


DEFAULT_CONFIG = {
    "ip": "http://192.168.1.10:1122",
    "opacity": 0.9,
    "font_size": 14,
    "font_family": "Microsoft YaHei",
    "line_spacing": LINE_SPACING_DEFAULT,
    "paragraph_spacing": 0,
    "page_margin": 0,          # 老键：像素值（有它就说明是老配置，载入时换算成下面那个）
    "page_margin_chars": PAGE_MARGIN_DEFAULT,   # 页边距：单位"字"，四边相同
    "first_line_indent": FIRST_LINE_INDENT_DEFAULT,
    "paragraph_indent": True,
    "page_overlap_lines": PAGE_OVERLAP_LINES,
    "arrow_scroll_lines": ARROW_SCROLL_LINES,
    # 标题永远不缩进；这个键只作为 txt 猜错标题时的手动开关，不在界面上
    "heading_no_indent": True,
    "peek_key": "ctrl+e",
    "text_color": "rgba(200, 200, 200, 255)",
    "bg_color": "rgba(30, 30, 30, 200)",
    "boss_key": "Esc",
    "safe_key": "ctrl+alt+d",            # 安全文档模式（有人看屏幕时应急换文档）
                                         # 不用 ctrl+shift+d：那键在 VSCode/PyCharm 是"运行与调试"、
                                         # 在 Chrome 是"收藏所有标签页"，而全局热键不拦截按键
    "safe_doc_path": "",                 # 留空 = 用内置的安全文档
    # --- 隐形背诵（把 Anki 卡片当成一份文档来背，见 docs/anki-review-design.md）---
    "anki_deck": "",                     # 默认牌组；留空 = 每次进模式时让你挑
    "anki_key": "ctrl+alt+a",            # 进/出隐形背诵
    "anki_swallow_keys": True,           # 背诵时吞掉 空格/回车/1-4/U/R/Esc（想正常打字就 Ctrl+D 收起）
    "anki_conceal": True,                # 进背诵时把 Anki 自己的窗口藏起来（SW_HIDE）
    "anki_api_key": "",                  # AnkiConnect 配了 apiKey 时填这里
    # --- 社区统一流（知乎/V2EX/微博…统一成一种帖子，当第三个内容页，见 docs/community-feed-design.md）---
    "feed_key": "ctrl+alt+f",            # 进/出社区统一流
    "feed_lowkey": False,                # 低调显示：社区页不出图、[图] 占位，工位上更不显眼
    "ghost_mode": False,
    "auto_mode": False,
    "antishot_mode": False,
    "window_width": 400,
    "window_height": 300,
    "last_local_file": "",
    "last_local_pos": 0
}

# 内置的安全文档：有人看到屏幕时顶上的那份"工作文档"。
# 体例要像日常工作产出（周报/复盘这类），内容必须完全无敏感信息，
# 一旦配置里指的文件读不出来，就回落这一份——任何时候都不能露出书页。
SAFE_DOC_TEXT = """服务端接口测试周报（第 15 周）

一、本周进展
1. 完成订单中心 v7 接口的用例补全：新增单接口用例 46 条、场景用例 12 条，接口覆盖率 61% 提升到 78%。
2. 测试环境治理：把 host、测试账号、密钥统一收敛到团队环境变量，用例里不再写死地址，换环境只需改一处。
3. 巡检接入执行集：每天 9:30 自动跑一遍核心链路，失败自动拉起群通知，本周拦下 3 次回归问题。
4. 补齐支付回调的幂等校验用例，覆盖重复通知、乱序通知、签名过期三类异常场景。

二、问题与风险
1. 灰度环境偶发超时（约 2% 请求），初步定位为下游依赖抖动，已在用例侧加 3 次重试与超时兜底，仍需下游确认。
2. 部分老接口文档与实现不一致（参数名、错误码），已整理成清单，待各模块负责人确认后统一更新。
3. 测试账号在下班时段会被风控拦截，导致夜间巡检误报，已申请加入白名单。

三、下周计划
1. 补齐退款链路的异常分支用例，重点覆盖部分退款、退款失败重试与对账差异。
2. 把覆盖率采集接进 CI，每次合并自动产出未覆盖接口清单。
3. 与前端对齐联调环境的 Mock 策略，减少相互等待。
4. 整理一份新同学上手说明，含环境变量、常用命令与排查手册。

四、需要协调
1. 退款链路的沙箱账号还缺一个，需要业务侧配合开通。
2. 下周三下午的环境维护窗口，请各模块提前确认无重要联调安排。

五、关键指标
接口覆盖率 61% 提升到 78%；场景用例通过率 96%（上周 92%）；
巡检累计拦下回归问题 3 次；用例平均执行时长 4 分 12 秒（上周 4 分 40 秒）。

六、下周排期
周一：同步灰度超时定位结论，输出待确认清单。
周二至周三：退款链路异常分支用例评审，前端 Mock 策略对齐。
周四：覆盖率接入 CI 联调，产出未覆盖接口清单。
周五：整理上手说明，汇总本周问题与风险，提交周报。

七、结论
整体进度符合预期，无阻塞项；灰度超时问题持续跟进，下周一同步进展。

（本页内容为内部工作记录，请勿外传）"""

DARK_STYLESHEET = """
    QDialog, QWidget { background-color: #2b2b2b; color: #cccccc; }
    QLineEdit { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px; border-radius: 4px; }
    QListWidget { background-color: #333; color: #ddd; border: 1px solid #444; }
    QListWidget::item:selected { background-color: #505050; color: white; }
    QListWidget::item:hover { background-color: #3e3e3e; }
    QPushButton { background-color: #444; color: white; border: 1px solid #555; padding: 5px; border-radius: 4px; }
    QPushButton:hover { background-color: #555; }
    QComboBox { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px; }
    QComboBox QAbstractItemView { background-color: #3c3c3c; color: white; selection-background-color: #505050; }
    QLabel { color: #aaa; }
"""


# 设置页专用样式：左边一列分区、右边一组一张卡片。只按 objectName 命中，
# 不动别处的控件（登录框、目录、牌组列表都是同一套 DARK_STYLESHEET）。
SETTINGS_QSS = """
    QWidget#settingsBody { background: #242424; }
    QListWidget#navList {
        background: #1f1f1f; border: none; border-right: 1px solid #333;
        padding: 10px 8px; outline: none;
    }
    QListWidget#navList::item {
        padding: 9px 12px; margin: 2px 0; border-radius: 6px; color: #b6b6b6;
    }
    QListWidget#navList::item:selected { background: #3a3a3a; color: #ffffff; }
    QListWidget#navList::item:hover { background: #2e2e2e; }
    QWidget#pageBody { background: #242424; }
    QScrollArea, QScrollArea > QWidget > QWidget { background: #242424; }
    QScrollBar:vertical {
        background: #242424; width: 10px; margin: 0; border: none;
    }
    QScrollBar::handle:vertical { background: #4a4a4a; border-radius: 5px; min-height: 30px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
    QFrame#card { background: #2d2d2d; border: 1px solid #383838; border-radius: 10px; }
    QLabel#cardTitle { color: #8ab4f8; font-weight: 600; }
    QLabel#rowLabel { color: #d2d2d2; }
    QFrame#footBar { background: #262626; border-top: 1px solid #333; }
    QPushButton#primaryButton {
        background: #3d6df0; border: 1px solid #3d6df0; color: #ffffff; padding: 6px 18px;
    }
    QPushButton#primaryButton:hover { background: #4a7bf8; border-color: #4a7bf8; }
    QPushButton { padding: 6px 14px; border-radius: 6px; }
    QSpinBox, QDoubleSpinBox {
        background: #3c3c3c; color: #ffffff; border: 1px solid #4a4a4a;
        border-radius: 5px; padding: 4px 6px;
    }
    QCheckBox { color: #d6d6d6; spacing: 8px; }
    QCheckBox::indicator {
        width: 15px; height: 15px; border-radius: 4px;
        border: 1px solid #5a5a5a; background: #333333;
    }
    QCheckBox::indicator:hover { border-color: #7a7a7a; }
    QCheckBox::indicator:checked { background: #3d6df0; border-color: #3d6df0; }
    QCheckBox::indicator:disabled { border-color: #3a3a3a; background: #2b2b2b; }
    QPushButton#miniButton {
        background: #383838; border: 1px solid #4a4a4a; color: #d6d6d6; padding: 3px 12px;
    }
    QPushButton#miniButton:hover { background: #434343; border-color: #5c5c5c; }
    QLabel#stateLabel { color: #9a9a9a; }
    QLabel#hintLabel { color: #8a8a8a; }
    QSlider::groove:horizontal { height: 4px; background: #444444; border-radius: 2px; }
    QSlider::handle:horizontal {
        width: 14px; margin: -6px 0; border-radius: 7px; background: #8ab4f8;
    }
"""


# ================= EPUB 解析（仅用标准库） =================
EPUB_SKIP_TAGS = {"script", "style", "head", "title", "meta", "link", "svg"}
EPUB_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
EPUB_BLOCK_TAGS = {
    "p", "div", "br", "hr", "li", "tr", "td", "th", "section", "article",
    "blockquote", "pre", "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
}


class _EpubHtmlText(HTMLParser):
    """把 XHTML 正文抽成纯文本行，记录每个 id/name 锚点落在哪一行，并标记标题行。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.ids = {}
        self.heading_lines = {}     # {行号: 标题级别}，级别取自 h1~h6 的编号
        self._buf = []
        self._skip = 0
        self._heading_stack = []    # 正在收集的标题标签（嵌套时取最外层那级）

    def _flush(self):
        line = re.sub(r"[ \t\f\v\u00a0\u3000]+", " ", "".join(self._buf)).strip()
        self._buf = []
        if line:
            if self._heading_stack:
                self.heading_lines.setdefault(len(self.lines), self._heading_stack[0])
            self.lines.append(line)

    def _mark(self, attrs):
        for key, value in attrs:
            if key in ("id", "name") and value:
                self.ids.setdefault(value, len(self.lines))

    def handle_starttag(self, tag, attrs):
        self._mark(attrs)
        if tag in EPUB_SKIP_TAGS:
            self._skip += 1
        elif tag in EPUB_BLOCK_TAGS:
            self._flush()
            if tag in EPUB_HEADING_TAGS:
                self._heading_stack.append(int(tag[1]))

    def handle_startendtag(self, tag, attrs):
        self._mark(attrs)
        if tag in EPUB_BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in EPUB_SKIP_TAGS:
            if self._skip:
                self._skip -= 1
        elif tag in EPUB_BLOCK_TAGS:
            self._flush()
            if tag in EPUB_HEADING_TAGS and self._heading_stack:
                self._heading_stack.pop()

    def handle_data(self, data):
        if self._skip:
            return
        for i, segment in enumerate(data.split("\n")):
            if i:
                self._flush()
            if segment:
                self._buf.append(segment)

    def finish(self):
        """返回 (纯文本, {锚点: 字符偏移}, {标题行字符偏移: 标题级别})。"""
        self._flush()
        text = "\n".join(self.lines)
        offsets = [0]
        for line in self.lines:
            offsets.append(offsets[-1] + len(line) + 1)
        anchors = {key: offsets[min(idx, len(self.lines))]
                   for key, idx in self.ids.items()}
        headings = {offsets[idx]: level for idx, level in self.heading_lines.items()}
        return text, anchors, headings


def _epub_local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _epub_html_to_text(raw_bytes):
    for enc in ("utf-8", "gb18030"):
        try:
            source = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            source = None
    if source is None:
        return "", {}, {}
    parser = _EpubHtmlText()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        pass
    return parser.finish()


EPUB_CHAPTER_TAIL = re.compile(
    r"^(?P<lead>.+?)\s*(?:CHAPTER\s*(?P<num>[IVXLCDM0-9]+)\s*\.?"
    r"|第\s*(?P<cn>[0-9一二三四五六七八九十百零]+)\s*[章节回])\s*$",
    re.IGNORECASE)


def _epub_clean_label(label):
    """有些 epub（如 Gutenberg 版）把插画说明和图注混进目录标题，这里只取末尾的章节号。"""
    label = " ".join((label or "").split())
    match = EPUB_CHAPTER_TAIL.match(label)
    if not match:
        return label
    if match.group("num"):
        return f"CHAPTER {match.group('num').upper()}."
    return f"第 {match.group('cn')} 章"


def _epub_toc_entries(zf, opf_path, manifest):
    """按文档顺序返回 [(zip 内路径, 锚点, 标题, 层级)]：优先 epub3 的 nav，其次 epub2 的 ncx。

    层级就是目录自己的嵌套深度（最外层 = 1），多级目录（卷 / 部 / 章 / 节）都收进来，
    目录页按层级缩进显示。"""
    entries = []
    opf_dir = posixpath.dirname(opf_path)
    nav_href = None
    ncx_href = None
    for item in manifest.values():
        if "nav" in (item.get("properties") or ""):
            nav_href = item["href"]
        if item.get("media-type") == "application/x-dtbncx+xml":
            ncx_href = item["href"]

    def resolve(href):
        base, _, fragment = (href or "").partition("#")
        target = posixpath.normpath(posixpath.join(opf_dir, unquote(base)))
        return target, fragment

    def read_xml(href):
        return ET.fromstring(zf.read(posixpath.normpath(posixpath.join(opf_dir, unquote(href)))))

    try:
        if nav_href:
            root = read_xml(nav_href)
            for nav in root.iter():
                if _epub_local_name(nav.tag) != "nav":
                    continue
                nav_type = (nav.get("{http://www.idpf.org/2007/ops}type")
                            or nav.get("{http://www.w3.org/1999/xhtml}type")
                            or nav.get("type") or "")
                if nav_type != "toc":
                    continue

                def walk(node, depth):
                    """递归走 nav 树：<ol>/<ul> 每嵌套一层，层级 +1；<a> 就是一条目录项。"""
                    for child in node:
                        name = _epub_local_name(child.tag)
                        if name == "a":
                            target, fragment = resolve(child.get("href"))
                            entries.append((target, fragment, "".join(child.itertext()), depth))
                        elif name in ("ol", "ul"):
                            walk(child, depth + 1)
                        else:
                            walk(child, depth)

                for child in nav:
                    walk(child, 0)
        elif ncx_href:
            root = read_xml(ncx_href)

            def walk_points(node, depth):
                """递归走 navMap/navPoint：navPoint 每嵌套一层，层级 +1。"""
                for child in node:
                    if _epub_local_name(child.tag) != "navPoint":
                        walk_points(child, depth)   # 中间可能夹着 navMap 之类的壳
                        continue
                    href, label = None, ""
                    for sub in child:   # navPoint 的直接子节点才是自己的 navLabel / content
                        name = _epub_local_name(sub.tag)
                        if name == "navLabel" and not label:
                            label = "".join(sub.itertext())
                        elif name == "content" and sub.get("src") and href is None:
                            href = sub.get("src")
                    target, fragment = resolve(href)
                    entries.append((target, fragment, label, depth))
                    walk_points(child, depth + 1)   # 子目录项

            walk_points(root, 1)
    except Exception:
        pass
    return [(t, f, l, lv) for t, f, l, lv in entries if l and l.strip()]


def _load_epub(path):
    """解析 epub，返回 (全文, [(章节标题, 起始字符偏移, 层级), ...], {标题行偏移})。"""
    with zipfile.ZipFile(path) as zf:
        opf_path = None
        try:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
            for node in container.iter():
                if _epub_local_name(node.tag) == "rootfile" and node.get("full-path"):
                    opf_path = node.get("full-path")
                    break
        except Exception:
            pass
        if not opf_path:
            for name in zf.namelist():
                if name.lower().endswith(".opf"):
                    opf_path = name
                    break
        if not opf_path:
            raise ValueError("找不到 OPF，不是有效的 epub")

        opf_dir = posixpath.dirname(opf_path)
        opf = ET.fromstring(zf.read(opf_path))

        manifest = {}
        spine_ids = []
        for node in opf.iter():
            name = _epub_local_name(node.tag)
            if name == "item":
                manifest[node.get("id")] = {
                    "href": node.get("href") or "",
                    "media-type": (node.get("media-type") or "").lower(),
                    "properties": node.get("properties") or "",
                }
            elif name == "itemref":
                if (node.get("linear") or "yes").lower() != "no":
                    spine_ids.append(node.get("idref"))

        toc = _epub_toc_entries(zf, opf_path, manifest)
        names = set(zf.namelist())

        docs = []  # [(zip 内路径, 该文件纯文本, {锚点: 字符偏移}, {标题行偏移})]
        for idref in spine_ids:
            item = manifest.get(idref)
            if not item or "html" not in item["media-type"]:
                continue
            target = posixpath.normpath(posixpath.join(opf_dir, unquote(item["href"].split("#")[0])))
            if target not in names:
                continue
            body, anchors, headings = _epub_html_to_text(zf.read(target))
            if body:
                docs.append((target, body, anchors, headings))

        if not docs:
            raise ValueError("epub 里没有可读文字（可能是纯图片书）")

        has_anchors = any(fragment for _target, fragment, _label, _level in toc)

        if toc and has_anchors:
            # 目录指向文件内的锚点：正文原样拼接，不额外插标题行，章节偏移由锚点换算
            doc_anchors = {target: anchors for target, _body, anchors, _heads in docs}
            blocks = []
            file_start = {}
            file_headings = {}
            cursor = 0
            for target, body, _anchors, heads in docs:
                file_start[target] = cursor
                file_headings[target] = heads
                piece = body.strip("\n") + "\n\n"
                blocks.append(piece)
                cursor += len(piece)

            chapters = []
            used_offsets = set()
            bodies = {target: body for target, body, _a, _h in docs}
            for target, fragment, label, level in toc:
                if target not in file_start:
                    continue
                offset = file_start[target] + doc_anchors.get(target, {}).get(fragment, 0)
                if offset in used_offsets:
                    continue
                used_offsets.add(offset)
                chapters.append((_epub_clean_label(label), offset, max(1, int(level or 1))))

            # 目录之外的小标题（h1~h6）也收进目录：级别用标签本身的编号，标题取那一行原文
            for target, offsets in file_headings.items():
                body = bodies.get(target, "")
                for offset, level in offsets.items():
                    absolute = file_start[target] + offset
                    if absolute in used_offsets:
                        continue
                    title = body[offset:].split("\n", 1)[0].strip()
                    if not title or len(title) > 80:
                        continue      # 空行、或者长到不像标题（有些书整段都包在 h3 里）
                    if len(title) > 60:
                        title = title[:60] + "…"
                    used_offsets.add(absolute)
                    chapters.append((title, absolute, max(1, int(level or 1))))
            chapters.sort(key=lambda item: item[1])

            # 标题行 = 原文里的 h1~h6，加上目录标题所在的那一行
            headings = {file_start[target] + offset
                        for target, offsets in file_headings.items()
                        for offset in offsets}
            headings.update(offset for _label, offset, _level in chapters)
            return "".join(blocks), chapters, headings

        # 目录缺失或只到文件级：一个文件一章，需要时补一行标题
        titles = {}
        for target, _fragment, label, _level in toc:
            titles.setdefault(target, _epub_clean_label(label))

        blocks = []
        chapters = []
        headings = set()
        cursor = 0
        for order, (target, body, _anchors, doc_headings) in enumerate(docs):
            first_line = body.split("\n", 1)[0].strip()
            title = titles.get(target, "")
            if not title:
                title = first_line if 0 < len(first_line) <= 60 else f"第 {order + 1} 节"

            prepended = first_line != title
            chunk = f"{title}\n\n{body}" if prepended else body
            piece = chunk.strip("\n") + "\n\n"

            chapters.append((title, cursor, 1))
            headings.add(cursor)  # 标题行本身
            shift = len(title) + 2 if prepended else 0
            used = {cursor}
            for offset, level in doc_headings.items():
                absolute = cursor + shift + offset
                if absolute in used:
                    continue      # 这一段的首行本身就是个标题：别和上面那条重名重位
                title = body[offset:].split("\n", 1)[0].strip()
                if not title or len(title) > 80:
                    continue
                used.add(absolute)
                chapters.append((title[:60], absolute, max(1, int(level or 1))))
                headings.add(absolute)
            blocks.append(piece)
            cursor += len(piece)

        chapters.sort(key=lambda item: item[1])
        return "".join(blocks), chapters, headings


def load_epub_with_headings(path):
    """解析 epub，额外返回标题行偏移（首行缩进要用它来跳过标题）。"""
    return _load_epub(path)


def load_epub_text(path):
    """解析 epub，返回 (全文, [(章节标题, 起始字符偏移, 层级), ...])。"""
    text, chapters, _headings = _load_epub(path)
    return text, chapters


# ================= txt 的标题识别（没有结构信息，只能按字面规则猜） =================
TXT_HEADING_PATTERN = re.compile(
    r"^(?:"
    r"第\s*[0-9０-９一二三四五六七八九十百千零两]+\s*[章节節回卷篇部集]"
    r"|(?:CHAPTER|PART|BOOK)\s+[IVXLCDM0-9]+\.?"
    r"|(?:序|序言|自序|序章|前言|引子|楔子|导读|后记|後記|尾声|尾聲|附录|附錄|目录|目錄"
    r"|版权|版權|献词|獻詞|致谢|致謝|译后记|譯後記|作者简介|作者簡介)(?:\s|$|[:：])"
    r")",
    re.IGNORECASE)
TXT_HEADING_MAX_LEN = 40
TXT_HEADING_TAIL = "。！？；：，、…—.!?;:,"


def normalize_chapter_levels(chapters):
    """让层级从 1 开始。有的书目录整棵都从第 2 层起（顶层是 h2），不归一化会整体歪一格。"""
    if not chapters:
        return chapters
    base = min(int(c[2]) for c in chapters)
    if base <= 1:
        return chapters
    return [(c[0], c[1], int(c[2]) - base + 1) for c in chapters]


def _txt_heading_level(line):
    """txt 没有层级信息，只能按关键词粗分：卷/部/篇 是上层，章/节/回 是下层。"""
    if re.match(r"^第\s*[0-9０-９一二三四五六七八九十百千零两]+\s*[卷部篇集]", line):
        return 1
    if re.match(r"^第\s*[0-9０-９一二三四五六七八九十百千零两]+\s*[章节節回]", line):
        return 2
    return 1


def txt_chapters_from_headings(text, offsets):
    """把猜出来的标题行整理成目录：[(标题, 偏移, 层级)]（层级归一化后从 1 起）。"""
    chapters = []
    for offset in sorted(offsets or []):
        line = text[offset:].split("\n", 1)[0].strip()
        if line:
            chapters.append((line[:60], int(offset), _txt_heading_level(line)))
    return normalize_chapter_levels(chapters)


def detect_txt_headings(text):
    """猜哪些行是标题：以"第X章/序/前言"这类词打头，行要短，且不以句末标点收尾。
    返回行的起始偏移集合（行首带空格时按行首算，和文本块的位置对齐）。"""
    offsets = set()
    position = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if (stripped and len(stripped) <= TXT_HEADING_MAX_LEN
                and not stripped.endswith(tuple(TXT_HEADING_TAIL))
                and TXT_HEADING_PATTERN.match(stripped)):
            offsets.add(position)
        position += len(line) + 1
    return offsets


# ================= Windows 防截屏 API 封装 =================
def set_window_protection(hwnd, enable=True):
    try:
        user32 = ctypes.windll.user32
        WDA_NONE = 0x00000000
        WDA_MONITOR = 0x00000001
        WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Win10 2004+

        mode = WDA_EXCLUDEFROMCAPTURE if enable else WDA_NONE
        user32.SetWindowDisplayAffinity(hwnd, mode)
    except Exception as e:
        print(f"防截屏设置失败: {e}")


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint),
                ("rcMonitor", ctypes.c_long * 4),
                ("rcWork", ctypes.c_long * 4),
                ("dwFlags", ctypes.c_uint)]


def window_on_real_screen(hwnd):
    """窗口是否落在某块"真实存在"的屏幕上。用 Win32 的 MonitorFromWindow 问系统，
    不经过 Qt——Qt 会缓存过期屏幕布局，拔过显示器后会把窗口安排到不存在的屏幕坐标里。

    必须声明原型：不声明时 ctypes 把 HWND 按 32 位 int 传、把 HMONITOR 按 32 位 int 收，
    64 位下句柄低 32 位碰巧为 0 就会被判成"不在任何屏幕上"，于是窗口被平白挪走。"""
    try:
        user32 = ctypes.windll.user32
        return bool(user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 0))  # MONITOR_DEFAULTTONULL
    except Exception:
        return True  # 校验失败时别乱动窗口


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def window_rect(hwnd):
    """窗口的物理像素矩形 (left, top, right, bottom)，失败返回 None。"""
    try:
        user32 = ctypes.windll.user32
        r = _RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def force_window_to_primary(hwnd, margin=60):
    """Win32 物理挪回主屏工作区左上角（绕过 Qt 可能过期的坐标映射，挪完 Qt 会自己同步回来）。"""
    try:
        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 1)  # MONITOR_DEFAULTTOPRIMARY
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if not hmon or not user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            return False
        x, y = mi.rcWork[0] + margin, mi.rcWork[1] + margin
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return True
    except Exception:
        return False


# ================= 轻量事件日志（排查老板键之类"偶尔失灵"的问题）=================
def log_event(msg):
    """追加一行到 main.py 旁边的 stealthreader.log，失败静默（日志不能反过来影响功能）。"""
    try:
        path = os.path.join(APP_DIR, "stealthreader.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ================= 划线高亮 / 书签数据（按书存放，与设置分开）=================
# 位置一律用"全文字符偏移"。本地正文就是 local_full_text 的一段纯切片，所以偏移在翻页、
# 改字号、改窗口大小、重启之后都仍然精确，不会漂。
ANNOTATION_COLORS = {
    "yellow": (255, 214, 102),
    "green": (140, 224, 140),
    "blue": (130, 190, 255),
    "pink": (255, 160, 200),
    "purple": (200, 160, 255),
}
ANNOTATION_COLOR_NAMES = [("yellow", "黄色"), ("green", "绿色"), ("blue", "蓝色"),
                          ("pink", "粉色"), ("purple", "紫色")]


# ================= Anki 卡片 → 我们自己的"文档"（隐形背诵） =================

def filter_anki_css(css):
    """卡片模板的 CSS 里只保留"像文档"的部分。

    保留：颜色、居中、表格、边距（Qt 支持 CSS 2.1 的一个子集）。
    丢掉：字号/字体（必须由阅读设置说了算，否则调了字号卡片不动，一眼看出不是同一份文档）、
    定位/flex/grid/宽高/动画（QTextEdit 不是浏览器，照着渲染只会走样，而**走样比空白更可疑**）。
    """
    if not css:
        return ""
    css = re.sub(r"(?is)/\*.*?\*/", "", css)
    drop = {"font-size", "font-family", "line-height", "position", "display", "float",
            "flex", "flex-direction", "justify-content", "align-items", "grid",
            "width", "height", "max-width", "min-width", "max-height", "min-height",
            "transform", "animation", "transition", "z-index", "overflow", "white-space",
            "content", "box-shadow", "filter"}

    def keep(m):
        selector, body = m.group(1).strip(), m.group(2)
        props = []
        for decl in body.split(";"):
            if ":" not in decl:
                continue
            prop = decl.split(":", 1)[0].strip().lower()
            if prop in drop or prop.startswith("-"):
                continue
            props.append(decl.strip())
        if not props or selector.startswith("@"):
            return ""
        return f"{selector} {{ {'; '.join(props)}; }}"

    return re.sub(r"(?is)([^{}]+)\{([^{}]*)\}", keep, css).strip()


def _strip_anki_furniture(html):
    """剥掉卡片模板的 CSS/JS，并清掉内联字号。

    实测：Anki 渲染的卡片 HTML 开头就是一大段 <style>（2000 多字符里大半是 CSS），必须整块拿掉；
    卡片里还带内联 style="font-size:20px"，不清掉的话"字号设置"对卡片时灵时不灵。
    """
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", "", html)
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", html)
    html = re.sub(r"(?is)<link[^>]*>", "", html)

    def fix_style(m):
        style = re.sub(r"(?i)(font-size|font-family|line-height|white-space)\s*:[^;\"']*;?", "",
                       m.group(1))
        style = style.strip().strip(";").strip()
        return f'style="{style}"' if style else ""

    html = re.sub(r'(?i)style="([^"]*)"', fix_style, html)
    html = re.sub(r"(?i)</?font[^>]*>", "", html)
    return html


def simplify_anki_html(html):
    """卡片 HTML → 能塞进 QTextEdit、并且像一份文档的 HTML。返回 (html, 音频文件名列表)。

    1. 剥掉 <style>/<script> 与内联字号（见 _strip_anki_furniture）；
    2. `[sound:xxx.mp3]` 从显示文本里删掉，文件名交给调用方——播放走 Anki 自己的
       `guiPlayAudio`（它懂 [sound:]、{{tts:}} 与自动播放策略，比我们自建播放器准得多）；
    3. `class="cloze"` 的挖空加粗显示（文本模式下挖空本来就是这个样子）。
    图片的 <img> 保留，由 QTextDocument 的 baseUrl 去 collection.media 里取。
    """
    if not html:
        return "", []
    sounds = re.findall(r"\[sound:([^\]]+)\]", html)
    html = re.sub(r"\[sound:[^\]]+\]", "", html)
    html = re.sub(r'(?is)<span[^>]*class="[^"]*\bcloze\b[^"]*"[^>]*>(.*?)</span>', r"<b>\1</b>", html)
    return _strip_anki_furniture(html).strip(), sounds


def annotations_file():
    return os.path.join(APP_DIR, "annotations.json")


def load_annotations():
    try:
        with open(annotations_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_annotations(data):
    """整份重写：先写临时文件再替换，避免写一半断电留下坏档。"""
    try:
        path = annotations_file()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log_event(f"保存划线/书签失败: {e}")
        return False


def highlight_color(name, alpha=95):
    r, g, b = ANNOTATION_COLORS.get(name, ANNOTATION_COLORS["yellow"])
    return QColor(r, g, b, alpha)


def make_dot_icon(name, checked=False, size=20):
    """色点：选中的当前色外加一圈白环。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = size / 2.0
    if checked:
        p.setPen(QPen(QColor(255, 255, 255, 235), 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(c, c), c - 1.5, c - 1.5)
    p.setPen(Qt.NoPen)
    p.setBrush(highlight_color(name, 255))
    p.drawEllipse(QPointF(c, c), c - 4.0, c - 4.0)
    p.end()
    return QIcon(pm)


def make_pen_icon(color_name, active, size=20):
    """划线 / 取消划线共用同一支笔，只靠状态区分：
    没划线时笔是灰的、下面那一道是虚线；已划线时笔提亮、那一道变成该划线的实色。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    body = QColor(232, 234, 237) if active else QColor(174, 180, 189)
    # 笔杆（斜的圆头粗线）+ 笔尖
    p.setPen(QPen(body, 4.0, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(7.0, 12.0), QPointF(12.6, 5.6))
    p.setPen(QPen(body, 2.2, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(5.6, 13.6), QPointF(7.4, 11.8))
    # 笔底下那一道就是"划线"本身
    if active:
        r, g, b = ANNOTATION_COLORS.get(color_name, ANNOTATION_COLORS["yellow"])
        p.setPen(QPen(QColor(r, g, b, 255), 3.2, Qt.SolidLine, Qt.RoundCap))
    else:
        p.setPen(QPen(QColor(174, 180, 189, 200), 2.0, Qt.DashLine, Qt.RoundCap))
    p.drawLine(QPointF(3.6, 17.2), QPointF(16.4, 17.2))
    p.end()
    return QIcon(pm)


# 现代感的深色弹出菜单（原生 QMenu 那套灰白边框太出戏）
MODERN_MENU_QSS = """
QMenu {
    background-color: rgba(30, 33, 38, 246);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 10px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 18px 7px 14px;
    border-radius: 7px;
    color: #e8eaed;
    font-size: 13px;
}
QMenu::item:selected { background-color: rgba(255, 255, 255, 34); color: #ffffff; }
QMenu::item:disabled { color: rgba(232, 234, 237, 90); }
QMenu::separator { height: 1px; background: rgba(255, 255, 255, 28); margin: 5px 10px; }
"""

PANEL_QSS = """
#card {
    background-color: rgba(32, 35, 40, 242);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 8px;
}
#card QPushButton {
    background-color: transparent;
    border: none;
    border-radius: 6px;
    color: #e6e9ed;
    font-size: 13px;
    padding: 5px 9px;
}
#card QPushButton:hover { background-color: rgba(255, 255, 255, 32); }
#card QPushButton:pressed { background-color: rgba(255, 255, 255, 50); }
#card QFrame[sep="1"] { background-color: rgba(255, 255, 255, 26); max-width: 1px; border: none; }
"""


def modernize_menu(menu):
    """给 QMenu 套上圆角深色皮（原生外观太"系统设置"了）。"""
    try:
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setStyleSheet(MODERN_MENU_QSS)
    except Exception:
        pass
    return menu


class HighlightActionPanel(QWidget):
    """划选或点中划线后浮出的小面板（非模态）。

    它本身就是"确认"这一步：点了色点才算划线。滚轮、点别处、按键盘、Esc 关掉都算
    不确认（hideEvent 里报 cancelled），这样手上多带一下不会被误当成划线。
    """

    chosen = pyqtSignal(str)        # "default" / "color:xxx" / "note" / "copy" / "delete"
    cancelled = pyqtSignal()
    wheel_scrolled = pyqtSignal(int)

    def __init__(self, parent=None, mode="new", current_color="yellow", has_note=False):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.mode = mode
        self._acted = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)   # 扁平：不留投影空间
        card = QFrame()
        card.setObjectName("card")

        row = QHBoxLayout(card)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(2)

        def add_text_btn(label, key, tip, icon=None):
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            if icon is not None:
                b.setIcon(icon)
                b.setIconSize(QSize(16, 16))
            b.clicked.connect(lambda _=False, k=key: self._fire(k))
            row.addWidget(b)
            return b

        # 划线 / 取消划线：同一个位置、同一支笔，只靠图标状态区分，不做成最右边另一个按钮
        marked = mode == "existing"
        add_text_btn("取消划线" if marked else "划线",
                     "delete" if marked else "default",
                     "取消这条划线（带笔记会再问一次）" if marked else "用上次的颜色划线",
                     icon=make_pen_icon(current_color, marked))
        for name, label in ANNOTATION_COLOR_NAMES:
            b = QPushButton()
            b.setFixedSize(26, 26)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"划成{label}" if mode == "new" else f"改成{label}")
            b.setIcon(make_dot_icon(name, name == current_color))
            b.setIconSize(QSize(18, 18))
            b.clicked.connect(lambda _=False, n=name: self._fire("color:" + n))
            row.addWidget(b)
        row.addWidget(self._separator())
        add_text_btn("编辑想法" if has_note else "想法", "note",
                     "编辑这条划线的笔记" if has_note else "划线并写点想法")
        add_text_btn("复制", "copy", "复制原文")
        outer.addWidget(card)
        self.setStyleSheet(PANEL_QSS)

    def _separator(self):
        sep = QFrame()
        sep.setProperty("sep", "1")
        sep.setFixedWidth(1)
        return sep

    def _fire(self, key):
        self._acted = True
        self.chosen.emit(key)

    def hideEvent(self, event):
        if not self._acted:
            self.cancelled.emit()
        super().hideEvent(event)

    def wheelEvent(self, event):
        # 滚轮 = 不确认，但滚轮本身照常翻页（交给窗口去补这一下）
        self.wheel_scrolled.emit(event.angleDelta().y())
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()          # Esc 关掉 = 不确认
        event.accept()


# ================= 低级输入钩子（偷瞄模式：全局滚轮翻页） =================
# 钩子回调由 Windows 在毫秒级内强拆超时者，所以这里只做判断和转发，
# 真正的翻页通过 Qt 队列信号回到主线程慢慢做。任何退出路径都必须调用 uninstall，
# 否则泄漏的钩子会让全系统滚轮发卡。
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_MOUSEWHEEL = 0x020A
# 鼠标按下类消息。0x0203 是"左键双击"不是中键（中键是 0x0207），这里以前标错了，
# 结果左键双击会被当成中键按下而退出偷瞄。
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONDBLCLK = 0x0206
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONDBLCLK = 0x0209

# 会话通知（锁屏 / 解锁）：解锁后 WH_KEYBOARD_LL 往往已经被系统悄悄摘掉，
# 见 check_hotkeys_alive() 的说明——所以把"解锁"当成重挂热键的明确触发点。
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0


class _MSG(ctypes.Structure):
    """Windows MSG（只用到 message / wParam 两个字段，其余照原样排布以对齐）。"""
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint32), ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long)]
WM_XBUTTONDOWN = 0x020B
# 非客户区（标题栏 / 边框 / 滚动条）另有一套消息，之前一个都没接，
# 所以点别人家窗口的标题栏也退不出偷瞄。
WM_NCLBUTTONDOWN = 0x00A1
WM_NCRBUTTONDOWN = 0x00A4
WM_NCMBUTTONDOWN = 0x00A8
# 只有右键 / 中键 / 侧键退出偷瞄。左键（含双击、含标题栏上的左键）一律放行且不退出：
# 偷瞄的时候还要能照常点工作窗口，点一下就被踢出去没法干活。
MOUSE_EXIT_MESSAGES = {WM_RBUTTONDOWN, WM_RBUTTONDBLCLK, WM_MBUTTONDOWN,
                       WM_MBUTTONDBLCLK, WM_XBUTTONDOWN,
                       WM_NCRBUTTONDOWN, WM_NCMBUTTONDOWN}

VK_MODIFIER_TOKENS = {"ctrl", "control", "alt", "shift", "win", "windows", "cmd", "menu"}
VK_CODES = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12, "shift": 0x10,
    "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
    "esc": 0x1B, "escape": 0x1B, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "scrolllock": 0x91, "pause": 0x13, "printscreen": 0x2C,
}
VK_CODES.update({f"f{i}": 0x6F + i for i in range(1, 13)})


def parse_hotkey_vk(text):
    """'ctrl+e' -> (主键 VK, {修饰键 VK})。认不出就返回 (None, set())，
    此时按键一律当"退出输入"放行，不吞键——宁可功能弱一点，不能吞掉用户的键。"""
    tokens = [t.strip().lower() for t in (text or "").split("+") if t.strip()]
    if not tokens:
        return None, set()
    mods, main = set(), None
    for token in tokens:
        vk = VK_CODES.get(token)
        if vk is None and len(token) == 1:
            vk = ord(token.upper())
        if vk is None:
            return None, set()
        if token in VK_MODIFIER_TOKENS:
            mods.add(vk)
        elif main is None:
            main = vk
        else:
            return None, set()
    if main is None:
        return None, set()
    return main, mods


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", _POINT), ("mouseData", ctypes.c_ulong), ("flags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


# 隐形背诵要吞掉的键（**只有这 9 个**，其余一律放行，免得妨碍人家干活）。
# 小键盘 1-4 也认（NumLock 开着时才是这几个 vk；关着时发的是 VK_END 之类，认不出来就不吞）。
REVIEW_KEY_NAMES = {
    0x20: "space", 0x0D: "enter", 0x1B: "esc", 0x55: "u", 0x52: "r",
    0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4",
    0x61: "1", 0x62: "2", 0x63: "3", 0x64: "4",
}
# 设置里关掉"偷瞄时也捕获空格/回车"之后，全局层仍然要收这些（评分与退出必须能收到）
REVIEW_KEY_NAMES_MIN = {"1", "2", "3", "4", "u", "r", "esc"}

# 偷瞄 + 社区页时归我们的键（vkCode → Qt 键码）。**不含 Esc**：Esc 仍然是"退出偷瞄"。
# 字母的 vkCode 正好等于 ASCII 大写，Qt 的 Qt.Key_A…Qt.Key_Z 也是这个值。
FEED_KEY_NAMES = {
    0x20: Qt.Key_Space, 0x0D: Qt.Key_Return, 0x08: Qt.Key_Backspace,
    0x26: Qt.Key_Up, 0x28: Qt.Key_Down,
    0x21: Qt.Key_PageUp, 0x22: Qt.Key_PageDown,
    0x24: Qt.Key_Home, 0x23: Qt.Key_End,
    0xDB: Qt.Key_BracketLeft, 0xDD: Qt.Key_BracketRight,
    0xBC: Qt.Key_Comma, 0xBE: Qt.Key_Period,
}
for _ch in "CEJKORVZ":
    FEED_KEY_NAMES[ord(_ch)] = getattr(Qt, "Key_" + _ch)

# 偷瞄 + 书页时归我们的键：翻页与逐行滚动。这些键以前在偷瞄里算"别的输入"，
# 一按就退出偷瞄——想翻页反而把窗口收了，于是"按空格把 ce 打破了"。
BOOK_KEY_NAMES = {
    0x20: "space", 0x21: "pageup", 0x22: "pagedown",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
}

# 窗口内的 Qt 键 → 复习键名。窗口内这一套走 keyPressEvent，不经过任何钩子：
# 窗口不是当前活动窗口就收不到，所以不会妨碍你在别的软件里打字。
# 空格/回车在窗口内**永远**是翻面键（不翻面就没法用）；设置项只管"偷瞄时要不要全局捕获"。
REVIEW_KEY_FROM_QT = {
    Qt.Key_Space: "space", Qt.Key_Return: "enter", Qt.Key_Enter: "enter",
    Qt.Key_U: "u", Qt.Key_R: "r", Qt.Key_Escape: "esc",
    Qt.Key_1: "1", Qt.Key_2: "2", Qt.Key_3: "3", Qt.Key_4: "4",
}
# Anki 的 nextReviews 里有 U+2066/U+2069 这类不可见控制符（`<10 分` 的双向隔离符），
# 某些字体会渲染成方框——显示前一律剔掉。
REVIEW_TEXT_JUNK = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

# 评分键的名字：Anki 按张数变（学习步/FSRS 下可能只有 2 个）
REVIEW_BUTTON_LABELS = {
    1: ["重来"],
    2: ["重来", "良好"],
    3: ["重来", "良好", "容易"],
    4: ["重来", "困难", "良好", "容易"],
}


_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)  # WPARAM/LPARAM
_user32 = ctypes.windll.user32
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
_user32.CallNextHookEx.restype = _LRESULT


class AnkiWorker(QThread):
    """AnkiConnect 调用的私有线程（常驻 + 队列串行）。

    实测：每个 `gui*` 动作固定 ~60ms——AnkiConnect 必须把它丢回 Anki 自己的主线程执行。
    放在主线程上做就是一卡一卡：2.5 秒对一次账＝每 2.5 秒冻 60ms，进牌组那一串调用
    更是一口气堵 0.7 秒。这里换成一条常驻线程串行执行（**串行**是为了保证评分顺序：
    绝不并发去答两张卡），结果用队列信号送回主线程，界面一个字节都不用等。
    """

    done = pyqtSignal(int, object)
    failed = pyqtSignal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = queue.Queue()
        self._stopping = False
        self.setObjectName("anki-worker")

    def submit(self, task_id, fn):
        self._queue.put((task_id, fn))

    def stop(self):
        self._stopping = True
        self._queue.put((None, None))

    def run(self):
        while not self._stopping:
            task_id, fn = self._queue.get()
            if task_id is None:
                break
            try:
                result = fn()
            except Exception as e:                      # 异常类型（AnkiBusy 等）原样送回主线程判定
                try:
                    self.failed.emit(task_id, e)
                except Exception:
                    pass
            else:
                try:
                    self.done.emit(task_id, result)
                except Exception:
                    pass


class GlobalInputHook:
    """**偷瞄模式专用**的全局输入钩子：滚轮翻页（吞掉，底下应用不滚），
    右键/中键/侧键与按键退出（放行），左键放行且不退出（偷瞄时要能照常点工作窗口）。

    只有一个层是全局的——**偷瞄（Ctrl+E）那一层**。窗口内的交互键（老板键 Ctrl+D、
    内容键 Ctrl+Alt+A / Ctrl+Alt+D、卡片交互键）一律走 Qt 自己的窗口内机制，钩子只在
    偷瞄期间装着；偷瞄中当前页是卡片时，卡片交互键也由这里全局捕获（可以不聚焦窗口背）。

    钩子装在 Qt 主线程上——Qt 的事件循环本来就在取消息，低级钩子的回调正好在其中
    触发。回调里只发 Qt 队列信号（QueuedConnection），绝不在钩子里直接翻页。"""

    def __init__(self, owner):
        self.owner = owner
        self._mouse_hook = None
        self._keyboard_hook = None
        self._peek_vk = None
        self._peek_mods = set()
        # 回调对象必须一直活着，被 GC 的钩子回调会直接崩掉进程
        self._mouse_proc = _HOOKPROC(self._on_mouse)
        self._keyboard_proc = _HOOKPROC(self._on_keyboard)

    def install(self, peek_key_text):
        self.uninstall()
        self._peek_vk, self._peek_mods = parse_hotkey_vk(peek_key_text)
        try:
            self._mouse_hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0)
            self._keyboard_hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._keyboard_proc, None, 0)
        except Exception as e:
            print(f"偷瞄模式钩子安装失败: {e}")
            self.uninstall()
        if not (self._mouse_hook and self._keyboard_hook):
            self.uninstall()
            return False
        return True

    def uninstall(self):
        for name in ("_mouse_hook", "_keyboard_hook"):
            handle = getattr(self, name, None)
            if handle:
                _user32.UnhookWindowsHookEx(handle)
                setattr(self, name, None)

    def _matches_peek_key(self, vk_code):
        if self._peek_vk is None or vk_code != self._peek_vk:
            return False
        for mod in self._peek_mods:
            if not (_user32.GetAsyncKeyState(mod) & 0x8000):
                return False
        return True

    def _on_mouse(self, n_code, w_param, l_param):
        try:
            if n_code >= 0 and self.owner.peek_mode:
                message = int(w_param)
                if message == WM_MOUSEWHEEL:
                    info = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                    self.owner.peek_wheel_signal.emit(delta)
                    return 1  # 吞掉滚轮：底下应用不滚，这里翻页
                if message in MOUSE_EXIT_MESSAGES:
                    self.owner.peek_exit_signal.emit("click")
                    return 0  # 右键/中键：退出，并把这次点击放行给下面的应用
                # 左键（含双击、含点标题栏/边框）：既不退出也不吞，直接放行
        except Exception:
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _on_keyboard(self, n_code, w_param, l_param):
        try:
            if n_code >= 0 and int(w_param) in (WM_KEYDOWN, WM_SYSKEYDOWN):
                info = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                if self.owner.peek_mode:
                    if self._matches_peek_key(int(info.vkCode)):
                        self.owner.peek_exit_signal.emit("peek_key")
                        return 1  # 偷瞄期间这个键归我们，别落到当前应用上
                    # 偷瞄这一层是全局的：当前页是卡片时，卡片交互键也在这里捕获——
                    # 不聚焦窗口也能翻面/评分（这是"ce 全局"的含义），而且不退出偷瞄。
                    if self.owner.peek_captures_review_keys():
                        name = REVIEW_KEY_NAMES.get(int(info.vkCode))
                        if name and name != "esc" and (
                                self.owner.review_swallow_all() or name in REVIEW_KEY_NAMES_MIN):
                            self.owner.review_key_signal.emit(name)
                            return 1
                    if self.owner.peek_captures_feed_keys():
                        # 偷瞄着看社区页时，c/v/e/z/j/k/空格… 这些是**我们自己的快捷键**：
                        # 收下来交给主线程（窗口不聚焦也能用），并不收起窗口。
                        # 以前这里一律"任意键 → 退出偷瞄"，按一下 C 窗口就没了。
                        fk = FEED_KEY_NAMES.get(int(info.vkCode))
                        if fk is not None:
                            self.owner.feed_key_signal.emit(int(fk))
                            return 1
                    if self.owner.peek_captures_book_keys():
                        # 偷瞄着看书：翻页键和方向键也是我们自己的，收了交给主线程，
                        # 窗口不聚焦也能翻，而且不退出偷瞄。
                        bk = BOOK_KEY_NAMES.get(int(info.vkCode))
                        if bk is not None:
                            self.owner.book_key_signal.emit(bk)
                            return 1
                    self.owner.peek_exit_signal.emit("key")
                    return 0  # 其他按键放行（也退出偷瞄）
        except Exception:
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)


# ================= 辅助类：绘制背景和角标 =================
class CornerFrame(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_auto_mode = False
        self.draw_corners = True          # 设置里的「显示角标」：关掉就是彻底干净的一页
        self.corner_color = QColor(128, 128, 128, 200)
        self.auto_bg_fill = QColor(0, 0, 0, 2)

    def set_mode(self, auto_mode):
        self.is_auto_mode = auto_mode
        self.update()

    def set_auto_bg_color(self, color):
        self.auto_bg_fill = QColor(color)
        self.auto_bg_fill.setAlpha(2)
        if self.is_auto_mode:
            self.update()

    def set_draw_corners(self, enable):
        self.draw_corners = bool(enable)
        self.update()

    def paintEvent(self, event):
        if not self.is_auto_mode:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.auto_bg_fill)

        if self.height() > 20 and self.draw_corners:
            painter.setPen(QPen(self.corner_color, 3))
            w, h = self.width(), self.height()
            length = 15
            painter.drawLine(0, 0, length, 0)
            painter.drawLine(0, 0, 0, length)
            painter.drawLine(w, h, w - length, h)
            painter.drawLine(w, h, w, h - length)


class ReaderTextEdit(QTextEdit):
    """正文区控件：鼠标能拖选，但光标永远是箭头，不长成"输入框"的样子。

    Qt 只要文本可选中（TextSelectableByMouse）就会在鼠标移动/进入/聚焦时把 viewport 的光标
    设成 I 形，所以每个可能被它改回来的入口之后都要把想要的形状压回去。
    想要的形状可临时换：右下角缩放区给缩放光标、中键拖动给移动光标。"""

    _pin_shape = Qt.ArrowCursor
    _PIN_EVENTS = (QEvent.MouseMove, QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                   QEvent.MouseButtonDblClick, QEvent.HoverMove, QEvent.Enter, QEvent.FocusIn)

    def set_pinned_cursor(self, shape):
        self._pin_shape = shape
        self._pin_cursor()

    def _pin_cursor(self):
        viewport = self.viewport()
        if viewport.cursor().shape() != self._pin_shape:
            viewport.setCursor(self._pin_shape)

    def event(self, event):
        result = super().event(event)
        if event.type() in self._PIN_EVENTS:
            self._pin_cursor()
        return result

    # 换文本、换交互标志、改只读也会让 Qt 重设光标，一并压回去
    def setPlainText(self, text):
        # 铺新文本前先把「当前字符格式」清空：上一次铺进正文区的可能是社区页（第一段就是
        # **加粗的帖子标题**）或背诵卡片，光标正停在那种字符上，它的 charFormat 会被 Qt
        # 带给整页新文本——真人看到的就是"进 CAF 再退出，正文全变成黑体加粗，像帖子标题"。
        # 字体那三层（控件/文档默认/字符）都可能是对的，所以只在字体上做文章的修法都治不住它。
        try:
            self.setCurrentCharFormat(QTextCharFormat())
        except Exception:
            pass
        super().setPlainText(text)
        self._pin_cursor()

    def setTextInteractionFlags(self, flags):
        super().setTextInteractionFlags(flags)
        self._pin_cursor()

    def setReadOnly(self, read_only):
        super().setReadOnly(read_only)
        self._pin_cursor()


# ================= 独立窗口：书籍选择器 =================
class BookSelector(QDialog):
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        # 直接点"X"关掉时没人给 selected_book 赋值，open_book_selector 里那句读它会 AttributeError
        # （取消 = 崩，实测过）。这里给个默认值，取消就是"什么都没选"。
        self.selected_book = None
        self.setWindowTitle("📚 书架")
        self.resize(400, 500)
        self.setStyleSheet(DARK_STYLESHEET + SETTINGS_QSS)
        self.initUI()
        self.populate_list(self.main_window.books)

    def initUI(self):
        layout = QVBoxLayout()
        top_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 搜索书名或作者...")
        self.search_input.textChanged.connect(self.filter_books)
        top_layout.addWidget(self.search_input)

        btn_refresh = QPushButton("🔄 刷新")
        btn_refresh.setFixedWidth(60)
        btn_refresh.clicked.connect(self.manual_refresh)
        top_layout.addWidget(btn_refresh)

        layout.addLayout(top_layout)
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        layout.addWidget(self.list_widget)
        self.setLayout(layout)

    def manual_refresh(self):
        self.setWindowTitle("📚 书架 (加载中...)")
        self.main_window.fetch_bookshelf_silent()

    def update_data(self, books):
        self.setWindowTitle(f"📚 书架 (共 {len(books)} 本)")
        current_search = self.search_input.text()
        if current_search:
            self.filter_books(current_search)
        else:
            self.populate_list(books)

    def populate_list(self, books_to_show):
        self.list_widget.clear()
        if not books_to_show: return
        for book in books_to_show:
            display_text = f"{book['name']} - {book['author']}"
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, book)
            self.list_widget.addItem(item)

    def filter_books(self, text):
        text = text.lower()
        filtered = []
        for book in self.main_window.books:
            if text in book['name'].lower() or text in book['author'].lower():
                filtered.append(book)
        self.populate_list(filtered)

    def on_item_double_clicked(self, item):
        self.selected_book = item.data(Qt.UserRole)
        self.accept()


# ================= 独立窗口：微信读书书架 =================
class WereadShelfLoader(QThread):
    """把书架取回来（网络活别放界面线程）。"""

    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, client):
        super().__init__()
        self.client = client

    def run(self):
        try:
            self.loaded.emit(self.client.shelf())
        except Exception as e:
            self.failed.emit(str(e))


class WereadBookLoader(QThread):
    """某一本的章节表 + 读到哪。章节表挂了就只报那一句，进度挂了不影响章节显示。"""

    loaded = pyqtSignal(list, dict)
    failed = pyqtSignal(str)

    def __init__(self, client, book_id):
        super().__init__()
        self.client = client
        self.book_id = book_id

    def run(self):
        try:
            chapters = self.client.chapters(self.book_id)
        except Exception as e:
            self.failed.emit(str(e))
            return
        try:
            prog = self.client.progress(self.book_id)
        except Exception:
            prog = {}
        self.loaded.emit(chapters, prog)


class WereadShelfDialog(QDialog):
    """📖 微信读书 · 我的书架：**这一层只到"读到哪了"**。

    给的是书名/作者/进度/当前章/"最后停在哪一行"，双击一本书去网页版接着读。
    **正文不在这里**：那要绕过微信读书专门防程序取正文的那套签名与校验（见 weread.py 开头），
    不做。所以这个窗口是"书架 + 我读到哪了"，不是一个阅读器——不假装。
    """

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.books = []
        self.current = None
        self.chapters = []
        self._loaders = []              # 还活着的取数线程（见 _spawn：跑着的 QThread 被回收会崩）
        self.setWindowTitle("📖 微信读书 · 我的书架")
        self.setStyleSheet(DARK_STYLESHEET)
        self.resize(780, 540)
        self.initUI()
        self.reload()

    # -- 线程 ------------------------------------------------------------------
    def _spawn(self, loader, on_ok, on_fail):
        """起一个取数线程，并保证它**跑着的时候不会被回收**。

        QThread 在自己还在跑的时候被销毁，Qt 会直接把进程 abort（实测：窗口一关，
        整个 StealthReader 没了，日志里就一句 "QThread: Destroyed while thread is still running"）。
        所以线程统一从这里起：记在 self._loaders 里，finished 之后才摘掉。"""
        self._loaders.append(loader)
        loader.loaded.connect(on_ok)
        loader.failed.connect(on_fail)
        loader.finished.connect(lambda l=loader: self._drop(l))
        loader.start()

    def _drop(self, loader):
        if loader in self._loaders:
            self._loaders.remove(loader)
        try:
            loader.deleteLater()
        except Exception:
            pass

    def _settle_loaders(self, ms=3000):
        """关窗口前等一等还在跑的取数（等不到也走，但至少别让它在跑的时候被销毁）。"""
        for loader in list(self._loaders):
            try:
                if loader.isRunning():
                    loader.wait(ms)
            except Exception:
                pass
        self._loaders = []

    def done(self, code):
        self._settle_loaders()
        super().done(code)

    def closeEvent(self, event):
        self._settle_loaders()
        super().closeEvent(event)

    def initUI(self):
        outer = QVBoxLayout()
        top = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 搜书名或作者…")
        self.search_input.textChanged.connect(self.filter_books)
        top.addWidget(self.search_input)
        outer.addLayout(top)

        middle = QHBoxLayout()
        self.list_widget = QListWidget()
        self.list_widget.setFixedWidth(340)
        self.list_widget.currentItemChanged.connect(self.on_book_picked)
        self.list_widget.itemDoubleClicked.connect(self.on_book_opened)
        middle.addWidget(self.list_widget)

        right = QVBoxLayout()
        self.info_label = QLabel("正在取书架…")
        self.info_label.setWordWrap(True)
        self.info_label.setAlignment(Qt.AlignTop)
        self.info_label.setStyleSheet("color: #cfcfcf;")
        right.addWidget(self.info_label)
        self.chapter_widget = QListWidget()
        self.chapter_widget.itemDoubleClicked.connect(self.on_chapter_opened)
        right.addWidget(self.chapter_widget)
        middle.addLayout(right)
        outer.addLayout(middle)

        bottom = QHBoxLayout()
        self.open_btn = QPushButton("🌐 去微信读书接着读")
        self.open_btn.clicked.connect(self.open_in_browser)
        self.open_btn.setEnabled(False)
        bottom.addWidget(self.open_btn)
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self.reload)
        bottom.addWidget(refresh_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        outer.addLayout(bottom)
        self.setLayout(outer)

    # -- 取数 -------------------------------------------------------------
    def reload(self):
        if not self.client.available:
            self.setWindowTitle("📖 微信读书 · 没有 Cookie")
            self.info_label.setText(
                "没有配微信读书的 Cookie。\n\n"
                "把浏览器里的 Cookie 写进 feed_auth.json 的 weread 里，例如：\n"
                '    {"weread": {"cookie": "wr_vid=…; wr_skey=…"}}\n\n'
                "装了 VS Code 的摸鱼插件的话，可以直接搬过来：\n"
                "    python tests\\import_touchfish_cookies.py --apply")
            return
        self.setWindowTitle("📖 微信读书 · 正在取书架…")
        self._spawn(WereadShelfLoader(self.client), self.on_shelf, self.on_failed)

    def on_shelf(self, books):
        self.books = books
        self.setWindowTitle("📖 微信读书 · 我的书架（%d 本）" % len(books))
        self.filter_books(self.search_input.text())
        if not books:
            self.info_label.setText("书架上没有书（或者这个 Cookie 对应的账号没有书架数据）。")

    def on_failed(self, msg):
        self.setWindowTitle("📖 微信读书 · 取数失败")
        self.info_label.setText("取书架失败：%s" % msg)
        log_event(f"微信读书：取书架失败 {msg}")

    def filter_books(self, text):
        text = (text or "").lower().strip()
        shown = [b for b in self.books
                 if not text or text in b["title"].lower() or text in b["author"].lower()]
        self.list_widget.clear()
        for b in shown:
            mark = "✅ " if b["finished"] else ""
            item = QListWidgetItem("%s%s · %s   %d%%" % (mark, b["title"], b["author"], b["progress"]))
            item.setData(Qt.UserRole, b)
            self.list_widget.addItem(item)
        if shown:
            self.list_widget.setCurrentRow(0)
        else:
            self.info_label.setText("没有匹配的书。")
            self.chapter_widget.clear()
            self.current = None
            self.open_btn.setEnabled(False)

    # -- 选中一本书 ---------------------------------------------------------
    def on_book_picked(self, item, _prev=None):
        if item is None:
            return
        book = item.data(Qt.UserRole)
        self.current = book
        self.open_btn.setEnabled(True)
        self.chapters = []
        self.chapter_widget.clear()
        self.info_label.setText("《%s》\n正在取章节和进度…" % book["title"])
        self._spawn(WereadBookLoader(self.client, book["id"]),
                    lambda chs, pr, b=book: self.on_book_detail(b, chs, pr),
                    self.on_chapter_failed)

    def on_book_detail(self, book, chapters, prog):
        if not self.current or self.current["id"] != book["id"]:
            return                      # 选中的书已经换了，这份结果作废
        self.chapters = chapters
        cur_uid = int(prog.get("chapter_uid") or book.get("chapter_uid") or 0)
        cur_idx = int(prog.get("chapter_idx") or book.get("chapter_idx") or 0)
        cur = None
        for c in chapters:
            if cur_uid and c["uid"] == cur_uid:
                cur = c
                break
        if cur is None and 0 < cur_idx <= len(chapters):
            cur = chapters[cur_idx - 1]
        pct = int(prog.get("progress") if prog.get("progress") is not None else book["progress"])
        lines = ["《%s》  %s" % (book["title"], book["author"]),
                 "读到 %d%%" % pct
                 + ("（第 %d 章，共 %d 章）" % (cur_idx, len(chapters)) if chapters else "")]
        mins = int((prog.get("reading_time") or book.get("reading_time") or 0))
        if mins:
            lines.append("累计阅读 %s" % _human_minutes(mins))
        if cur:
            lines.append("当前章：%s" % cur["title"])
        if prog.get("summary"):
            lines.append("最后停在这一行：%s" % prog["summary"])
        lines.append("")
        lines.append("下面是目录（▶ 是读到的那一章）。正文不在这里：微信读书的正文要绕过它自己的"
                     "保护才拿得到，所以这个窗口只帮你记住读到哪、并送你去网页版接着读。")
        self.info_label.setText("\n".join(lines))
        self.chapter_widget.clear()
        for c in chapters:
            prefix = "▶ " if (cur and c["uid"] == cur["uid"]) else "   "
            words = ("%d 字" % c["words"]) if c["words"] else ""
            lock = "🔒 " if c["paid"] else ""
            row = QListWidgetItem("%s%s%s%s" % (prefix, lock, c["title"], ("   " + words) if words else ""))
            row.setData(Qt.UserRole, c)
            self.chapter_widget.addItem(row)
            if cur and c["uid"] == cur["uid"]:
                self.chapter_widget.setCurrentItem(row)
        if cur:
            self.chapter_widget.scrollToItem(self.chapter_widget.currentItem())

    def on_chapter_failed(self, msg):
        self.info_label.setText("取章节失败：%s" % msg)

    # -- 去网页版读 ---------------------------------------------------------
    def open_in_browser(self):
        book = self.current
        if not book:
            return
        url = book.get("url") or ""
        if not url:
            return
        try:
            os.startfile(url)
            log_event(f"微信读书：打开 {book['title']}")
        except Exception as e:
            log_event(f"微信读书：打开失败 {e}")

    def on_book_opened(self, item):
        self.on_book_picked(item)
        self.open_in_browser()

    def on_chapter_opened(self, item):
        # 章节这一列只是"目录 + 读到哪"，双击交回给他自己选的浏览器页面
        self.open_in_browser()


def _human_minutes(seconds):
    """秒 → "1 小时 25 分" / "12 分" / "不到 1 分"（书架里那个 readingTime 是秒）。"""
    if seconds < 60:
        return "不到 1 分"
    m = int(seconds // 60)
    if m < 60:
        return "%d 分" % m
    return "%d 小时 %d 分" % (m // 60, m % 60)


# ================= 独立窗口：目录选择器 =================
class ChapterLoader(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, ip, book_url):
        super().__init__()
        self.ip = ip
        self.book_url = book_url

    def run(self):
        try:
            url = f"{self.ip}/getChapterList"
            res = requests.get(url, params={"url": self.book_url}, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data['isSuccess']:
                    self.loaded.emit(data['data'])
                else:
                    self.failed.emit(data.get('errorMsg', '未知错误'))
            else:
                self.failed.emit(f"HTTP {res.status_code}")
        except Exception as e:
            self.failed.emit(str(e))


class TocSelector(QDialog):
    def __init__(self, ip, book_url, current_index, cached_toc=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📖 目录加载中...")
        self.resize(400, 600)
        self.ip = ip
        self.book_url = book_url
        self.selected_index = None
        self.main_window = parent
        self.target_index = current_index
        self.loader = None
        self.setStyleSheet(DARK_STYLESHEET)

        self.initUI()

        if cached_toc and len(cached_toc) > 0:
            self.on_loaded(cached_toc)
        else:
            self.loader = ChapterLoader(ip, book_url)
            self.loader.loaded.connect(self.on_loaded)
            self.loader.failed.connect(self.on_failed)
            self.loader.start()

    def initUI(self):
        layout = QVBoxLayout()
        self.status_label = QLabel("正在从手机获取目录...")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.list_widget.hide()
        layout.addWidget(self.list_widget)
        self.setLayout(layout)

    def on_loaded(self, chapters):
        try:
            self.setWindowTitle(f"📖 目录 (共 {len(chapters)} 章)")
            self.status_label.hide()
            self.list_widget.show()

            if self.main_window:
                self.main_window.current_toc = chapters

            for i, chapter in enumerate(chapters):
                title = str(chapter.get('title', f'第 {i + 1} 章'))
                item = QListWidgetItem(title)
                idx = chapter.get('index', i)
                item.setData(Qt.UserRole, idx)
                self.list_widget.addItem(item)
                if i == self.target_index:
                    item.setSelected(True)
                    self.list_widget.scrollToItem(item, QListWidget.PositionAtCenter)
        except Exception as e:
            self.status_label.setText(f"数据解析错误: {str(e)}")
            self.status_label.show()

    def on_failed(self, msg):
        self.status_label.setText(f"目录加载失败: {msg}")

    def on_item_double_clicked(self, item):
        self.selected_index = item.data(Qt.UserRole)
        self.accept()

    def closeEvent(self, event):
        if self.loader and self.loader.isRunning():
            self.loader.terminate()
            self.loader.wait()
        super().closeEvent(event)


# ================= 独立窗口：本地书籍目录（epub） =================
class ReadingMarksDialog(QDialog):
    """目录 / 书签 / 划线 三合一：双击跳转、右键删除、划线页可导出。"""

    def __init__(self, window, initial_tab=0):
        super().__init__(window)
        self.win = window
        self.jump_offset = None
        self.setWindowTitle("📑 目录 / 书签 / 划线")
        self.resize(540, 640)
        self.setStyleSheet(DARK_STYLESHEET)

        layout = QVBoxLayout()
        self.tabs = QTabWidget()
        self.toc_list = self._make_list()
        self.bookmark_list = self._make_list()
        self.highlight_list = self._make_list()
        self.tabs.addTab(self.toc_list, "目录")
        self.tabs.addTab(self.bookmark_list, "书签")
        self.tabs.addTab(self.highlight_list, "划线")
        self.tabs.setCurrentIndex(max(0, min(2, int(initial_tab))))
        layout.addWidget(self.tabs)

        bottom = QHBoxLayout()
        export_btn = QPushButton("📤 导出划线笔记 (Markdown)")
        export_btn.clicked.connect(self.on_export)
        bottom.addWidget(export_btn)
        hint = QLabel("双击跳转 · 右键删除")
        hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bottom.addWidget(hint)
        layout.addLayout(bottom)
        self.setLayout(layout)
        self.refresh()

    def _make_list(self):
        lst = QListWidget()
        lst.itemDoubleClicked.connect(self.on_item_double_clicked)
        lst.setContextMenuPolicy(Qt.CustomContextMenu)
        lst.customContextMenuRequested.connect(self.on_context_menu)
        return lst

    def refresh(self):
        self.toc_list.clear()
        for entry in (self.win.local_chapters or []):
            title, offset = entry[0], entry[1]
            level = int(entry[2]) if len(entry) > 2 else 1
            level = max(1, int(level or 1))
            # 层级用全角空格缩进（缩进宽度跟着页面边距走，导出时用的是原始标题，不受影响）
            item = QListWidgetItem("　" * (level - 1) + str(title).strip())
            item.setData(Qt.UserRole, ("jump", int(offset)))
            item.setData(Qt.UserRole + 2, level)
            item.setToolTip(str(title).strip() + (f"（第 {level} 级）" if level > 1 else ""))
            self.toc_list.addItem(item)
        if self.toc_list.count() == 0:
            self.toc_list.addItem(QListWidgetItem("（这本书没有章节目录，txt 常见）"))

        self.bookmark_list.clear()
        for bm in self.win.bookmarks_for_book():
            item = QListWidgetItem(f"🔖 {bm.get('chapter') or '正文'} · {bm.get('snippet', '')}")
            item.setData(Qt.UserRole, ("jump", int(bm.get("pos", 0))))
            item.setData(Qt.UserRole + 1, ("bookmark", bm.get("id")))
            item.setToolTip(f"{bm.get('created', '')}\n{bm.get('snippet', '')}")
            self.bookmark_list.addItem(item)
        if self.bookmark_list.count() == 0:
            self.bookmark_list.addItem(QListWidgetItem("（还没有书签：窗口里按 Ctrl+B 收藏本页）"))

        self.highlight_list.clear()
        for hl in self.win.highlights_for_book():
            text = " ".join(str(hl.get("text", "")).split())
            if len(text) > 42:
                text = text[:42] + "…"
            note = hl.get("note") or ""
            label = ("📝 " if note else "") + text + ("　（已失效）" if hl.get("orphan") else "")
            item = QListWidgetItem(label)
            item.setIcon(self.win.color_icon(hl.get("color", "yellow")))
            item.setData(Qt.UserRole, ("jump", int(hl.get("start", 0))))
            item.setData(Qt.UserRole + 1, ("highlight", hl.get("id")))
            item.setToolTip((f"笔记：{note}\n" if note else "") + str(hl.get("text", "")))
            self.highlight_list.addItem(item)
        if self.highlight_list.count() == 0:
            self.highlight_list.addItem(QListWidgetItem("（还没有划线：窗口里拖选文字，松手即成高亮）"))

    def on_item_double_clicked(self, item):
        data = item.data(Qt.UserRole)
        if not data:
            return
        kind, value = data
        if kind == "jump":
            self.jump_offset = int(value)
            self.accept()

    def on_context_menu(self, pos):
        lst = self.sender()
        item = lst.itemAt(pos)
        if not item:
            return
        tag = item.data(Qt.UserRole + 1)
        if not tag:
            return
        kind, ident = tag
        menu = QMenu(self)
        menu.addAction("📍 跳转到这里").triggered.connect(lambda: self.on_item_double_clicked(item))
        if kind == "bookmark":
            menu.addAction("🗑 删除书签").triggered.connect(
                lambda: (self.win.delete_bookmark(ident), self.refresh()))
        else:
            menu.addAction("🗑 删除划线").triggered.connect(
                lambda: (self.win.delete_highlight(ident), self.refresh()))
        modernize_menu(menu)
        menu.exec_(lst.mapToGlobal(pos))
        menu.deleteLater()

    def on_export(self):
        self.win.export_annotations()


# ================= 设置窗口 =================
class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.main_window = parent
        self.original_opacity = self.config.get("opacity", 0.9)
        self.temp_text_color = self.config.get("text_color")
        self.temp_bg_color = self.config.get("bg_color")
        # 排版是"拖动即预览"：先把原值记下来，点取消要能退回去，否则取消等于偷偷保存
        self.original_typography = {
            key: self.config.get(key) for key in
            ("font_size", "font_family", "line_spacing", "paragraph_spacing",
             "page_margin_chars", "first_line_indent", "paragraph_indent",
             "feed_font_size", "feed_font_family")
            # 注意 **不包含 arrow_scroll_lines**（滚动行数）：它不是"排版预览"，
            # 屏幕上没有预览可退；以前点「取消」会把它悄悄改回旧值再落盘，
            # 表现出来就是"调了滚动行数没生效"（真人报过两次）。
        }

        self.cookie_edits = {}      # 这一轮贴/找到的 Cookie：点保存才写进 feed_auth.json
        self.auth_rows = {}
        self.source_checks = {}
        self.setWindowTitle("设置")
        self.resize(350, 560)
        self.setStyleSheet(DARK_STYLESHEET)
        self.initUI()

    def initUI(self):
        """设置页：左边一列分区，右边一组一张卡片。

        界面上只出现标签本身，解释一律进 tooltip（真人："不要把注释性语言当成前端展示语言"）。
        每组之间用一条细边卡片分开，一眼能看出哪几项是一回事；底部只留「取消 / 保存」。
        """
        self.setWindowTitle("设置")
        self.resize(790, 640)
        self.setMinimumSize(640, 500)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QWidget()
        body.setObjectName("settingsBody")
        body_row = QHBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("navList")
        self.nav_list.setFixedWidth(150)
        self.nav_list.setFocusPolicy(Qt.NoFocus)
        self.nav_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("pageStack")
        body_row.addWidget(self.nav_list)
        body_row.addWidget(self.page_stack, 1)
        root.addWidget(body, 1)

        bar = QFrame()
        bar.setObjectName("footBar")
        bar_row = QHBoxLayout(bar)
        bar_row.setContentsMargins(16, 10, 16, 10)
        bar_row.setSpacing(10)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("保存")
        btn_save.setObjectName("primaryButton")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self.accept)
        bar_row.addStretch(1)
        bar_row.addWidget(btn_cancel)
        bar_row.addWidget(btn_save)
        root.addWidget(bar)

        self.build_pages()
        self.nav_list.currentRowChanged.connect(self.page_stack.setCurrentIndex)
        self.nav_list.setCurrentRow(0)
        self.on_auto_mode_toggled(self.check_auto_mode.isChecked())
        self.on_antishot_toggled(self.check_antishot.isChecked())

    # --- 版面构件（只在本对话框里用，所以不往上提） ---
    def new_page(self, title):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        holder.setObjectName("pageBody")
        col = QVBoxLayout(holder)
        col.setContentsMargins(18, 16, 18, 16)
        col.setSpacing(14)
        scroll.setWidget(holder)
        self.nav_list.addItem(QListWidgetItem(title))
        self.page_stack.addWidget(scroll)
        return col

    def new_card(self, col, title=""):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 13, 16, 13)
        lay.setSpacing(9)
        if title:
            head = QLabel(title)
            head.setObjectName("cardTitle")
            lay.addWidget(head)
        col.addWidget(card)
        return lay

    def add_row(self, lay, label, widget, hint=""):
        row = QWidget()
        row.setObjectName("formRow")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        name = QLabel(label)
        name.setObjectName("rowLabel")
        name.setFixedWidth(108)
        h.addWidget(name)
        h.addWidget(widget, 1)
        lay.addWidget(row)
        if hint:
            row.setToolTip(hint)
            name.setToolTip(hint)
            widget.setToolTip(hint)
        return row

    def add_wide(self, lay, widget, hint=""):
        lay.addWidget(widget)
        if hint:
            widget.setToolTip(hint)
        return widget

    def build_pages(self):
        self.build_page_read()
        self.build_page_window()
        self.build_page_keys()
        self.build_page_card()
        self.build_page_feed()
        self.build_page_ui()
        self.build_page_privacy()
        self.build_page_source()

    def build_page_read(self):
        col = self.new_page("阅读")

        lay = self.new_card(col, "文字")
        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 60)
        self.font_spin.setValue(self.config.get("font_size"))
        self.font_spin.valueChanged.connect(self.on_font_change)
        self.add_row(lay, "字号", self.font_spin)

        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(QFont(self.config.get("font_family", "Microsoft YaHei")))
        self.font_combo.currentFontChanged.connect(self.on_font_change)
        self.add_row(lay, "字体", self.font_combo)

        lay = self.new_card(col, "排版")
        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(LINE_SPACING_MIN, LINE_SPACING_MAX)
        self.spacing_spin.setSingleStep(5)
        self.spacing_spin.setSuffix(" %")
        self.spacing_spin.setValue(clamp_config_int(self.config, "line_spacing", LINE_SPACING_DEFAULT,
                                                    LINE_SPACING_MIN, LINE_SPACING_MAX))
        self.spacing_spin.valueChanged.connect(self.on_typography_change)
        self.add_row(lay, "行距", self.spacing_spin,
                     "行高相对字号的百分比。100% 是原始行距，150% 约等于一行字配半行空白。")

        self.para_spin = QSpinBox()
        self.para_spin.setRange(PARA_SPACING_MIN, PARA_SPACING_MAX)
        self.para_spin.setSingleStep(10)
        self.para_spin.setSuffix(" %")
        self.para_spin.setValue(clamp_config_int(self.config, "paragraph_spacing", 0,
                                                 PARA_SPACING_MIN, PARA_SPACING_MAX))
        self.para_spin.valueChanged.connect(self.on_typography_change)
        self.add_row(lay, "段间距", self.para_spin,
                     "段与段之间的额外空白，同样以行高为基准，0% 为原始效果。\n"
                     "若 txt 里每行都是硬换行，段间距会看起来像额外的行距。")

        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(PAGE_MARGIN_MIN, PAGE_MARGIN_MAX)
        self.margin_spin.setSingleStep(0.1)
        self.margin_spin.setDecimals(2)
        self.margin_spin.setSuffix(" 字")
        initial_margin = (self.main_window.page_margin_chars() if self.main_window
                          else clamp_config_number(self.config, "page_margin_chars",
                                                  PAGE_MARGIN_DEFAULT,
                                                  PAGE_MARGIN_MIN, PAGE_MARGIN_MAX))
        self.margin_spin.setValue(initial_margin)
        self.margin_spin.valueChanged.connect(self.on_typography_change)
        self.add_row(lay, "页边距", self.margin_spin,
                     "文字四周的留白（上下左右相同），单位是「字」——1 字 = 一个全角空格宽，\n"
                     "随字号字体等比变化。窗口很窄时别设太大，会挤掉每行字数。")

        self.arrow_spin = QSpinBox()
        self.arrow_spin.setRange(1, ARROW_SCROLL_MAX)
        self.arrow_spin.setSuffix(" 行")
        self.arrow_spin.setValue(clamp_config_int(self.config, "arrow_scroll_lines",
                                                 ARROW_SCROLL_LINES, 1, ARROW_SCROLL_MAX))
        self.arrow_spin.valueChanged.connect(self.on_typography_change)
        self.add_row(lay, "滚动行数", self.arrow_spin,
                     "上下箭头和鼠标滚轮每次滚动的行数（滚轮转一格 = 这么多行）。\n"
                     "整页翻仍然由空格 / PageUp / PageDown / 左右箭头负责。")

        self.para_indent_check = QCheckBox("段落缩进")
        self.para_indent_check.setChecked(bool(self.config.get("paragraph_indent", True)))
        self.para_indent_check.toggled.connect(self.on_typography_change)
        self.add_wide(lay, self.para_indent_check,
                      "给每个自然段的第一行加缩进；标题（章节名、序、前言这类行）自动不缩进。\n"
                      "不想要缩进也可以把下面的「首行缩进」调到 0。")

        self.indent_spin = QDoubleSpinBox()
        self.indent_spin.setRange(FIRST_LINE_INDENT_MIN, FIRST_LINE_INDENT_MAX)
        self.indent_spin.setSingleStep(0.5)
        self.indent_spin.setDecimals(1)
        self.indent_spin.setSuffix(" 字")
        self.indent_spin.setValue(clamp_config_number(self.config, "first_line_indent",
                                                      FIRST_LINE_INDENT_DEFAULT,
                                                      FIRST_LINE_INDENT_MIN, FIRST_LINE_INDENT_MAX))
        self.indent_spin.valueChanged.connect(self.on_typography_change)
        self.add_row(lay, "首行缩进", self.indent_spin,
                     "缩进多少，1 字符 = 一个全角空格的宽度（随字号字体变化）。0 就是不缩进。")

        col.addStretch(1)

    def build_page_window(self):
        col = self.new_page("窗口")

        lay = self.new_card(col, "外观")
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(10, 100)
        self.opacity_slider.setValue(int(self.config.get("opacity") * 100))
        self.opacity_slider.valueChanged.connect(self.on_opacity_change)
        self.add_row(lay, "不透明度", self.opacity_slider)

        self.check_auto_mode = QCheckBox("自动挡")
        self.check_auto_mode.setChecked(self.config.get("auto_mode", False))
        self.check_auto_mode.toggled.connect(self.on_auto_mode_toggled)
        self.add_wide(lay, self.check_auto_mode,
                      "背景取窗口后方的颜色，字体颜色自动反转，窗口几乎隐形。\n"
                      "只在需要贴着别的窗口读的时候打开；关掉就是正常的深色底。")

        self.check_ghost_mode = QCheckBox("幽灵模式")
        self.check_ghost_mode.setChecked(self.config.get("ghost_mode", False))
        self.add_wide(lay, self.check_ghost_mode,
                      "鼠标移出窗口就淡到几乎透明，回来立刻恢复。")

        lay = self.new_card(col, "颜色")
        self.btn_text_color = QPushButton("选择文字颜色")
        self.btn_text_color.setStyleSheet(f"background-color: {self.temp_text_color};")
        self.btn_text_color.clicked.connect(self.pick_text_color)
        self.btn_bg_color = QPushButton("选择背景颜色")
        self.btn_bg_color.setStyleSheet(f"background-color: {self.temp_bg_color};")
        self.btn_bg_color.clicked.connect(self.pick_bg_color)
        color_row = QWidget()
        color_row.setObjectName("formRow")
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(10)
        color_layout.addWidget(self.btn_text_color, 1)
        color_layout.addWidget(self.btn_bg_color, 1)
        self.add_row(lay, "配色", color_row)

        col.addStretch(1)

    def build_page_keys(self):
        col = self.new_page("快捷键")

        lay = self.new_card(col, "模式切换")
        self.boss_key_input = QLineEdit(self.config.get("boss_key", "Esc"))
        self.add_row(lay, "老板键", self.boss_key_input,
                     "显示/隐藏窗口。写法例如 Esc、ctrl+s、ctrl+alt+s。")

        self.peek_key_input = QLineEdit(self.config.get("peek_key", "ctrl+e"))
        self.add_row(lay, "偷瞄", self.peek_key_input,
                     "按这个键浮现书页，鼠标滚轮在任何位置都能翻页，\n"
                     "敲键盘 / 右键 / 中键则退出并藏回去；左键点击不退出。\n"
                     "热键本身不会被发给当前应用。")

        self.anki_key_input = QLineEdit(self.config.get("anki_key", "ctrl+alt+a"))
        self.add_row(lay, "卡片", self.anki_key_input,
                     "按这个键：书页换成 Anki 卡片（还是这本书的字体/字号/行距/页边距）。\n"
                     "再按一次退出，书回到原来那一页，阅读进度一个字节都不变。")

        self.feed_key_input = QLineEdit(self.config.get("feed_key", "ctrl+alt+f"))
        self.add_row(lay, "社区", self.feed_key_input,
                     "按这个键：书页换成社区统一流（知乎/V2EX/微博…所有来源都是同一种帖子）。\n"
                     "再按一次退出，书回到原来那一页，阅读进度一个字节都不变。")

        self.safe_key_input = QLineEdit(self.config.get("safe_key", "ctrl+alt+d"))
        self.add_row(lay, "安全文档", self.safe_key_input,
                     "有人凑过来看屏幕时按这个键，书页立刻换成一份预置的工作文档，\n"
                     "排版（字体/行距/页边距/窗口大小）和书页完全一致；\n"
                     "再按一次换回书，回到原来那一页。划线会一起隐藏。")

        col.addStretch(1)

    def build_page_card(self):
        col = self.new_page("卡片")

        lay = self.new_card(col, "连接")
        deck_row = QWidget()
        deck_row.setObjectName("formRow")
        deck_layout = QHBoxLayout(deck_row)
        deck_layout.setContentsMargins(0, 0, 0, 0)
        deck_layout.setSpacing(8)
        self.anki_deck_input = QLineEdit(self.config.get("anki_deck", ""))
        self.anki_deck_input.setPlaceholderText("牌组名，例如 背诵")
        self.anki_deck_input.setToolTip("要背哪个牌组（子牌组会自动包含在内）。")
        btn_pick_deck = QPushButton("列出牌组…")
        btn_pick_deck.clicked.connect(self.pick_anki_deck)
        deck_layout.addWidget(self.anki_deck_input, 1)
        deck_layout.addWidget(btn_pick_deck)
        self.add_row(lay, "牌组", deck_row,
                     "从正在运行的 Anki 里读出牌组（带今日待复习张数）用右边这个按钮。")

        key_row = QWidget()
        key_row.setObjectName("formRow")
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(8)
        self.anki_api_key_input = QLineEdit(self.config.get("anki_api_key", ""))
        self.anki_api_key_input.setPlaceholderText("留空 = AnkiConnect 没设 apiKey")
        self.anki_api_key_input.setEchoMode(QLineEdit.Password)
        btn_test_anki = QPushButton("测试连接")
        btn_test_anki.clicked.connect(self.test_anki_connection)
        key_layout.addWidget(self.anki_api_key_input, 1)
        key_layout.addWidget(btn_test_anki)
        self.add_row(lay, "AnkiConnect", key_row,
                     "AnkiConnect 配置里若设了 apiKey，填在这里；没设就留空。")

        lay = self.new_card(col, "行为")
        self.anki_swallow_check = QCheckBox("偷瞄时也捕获空格/回车")
        self.anki_swallow_check.setChecked(bool(self.config.get("anki_swallow_keys", True)))
        self.add_wide(lay, self.anki_swallow_check,
                      "偷瞄（Ctrl+E）那一层是全局的：勾上则空格/回车也归卡片（翻面/继续），\n"
                      "关掉则它们留给你手头的软件打字。窗口内不受影响。\n"
                      "评分键 1-4 一定捕获。")

        self.anki_conceal_check = QCheckBox("背诵时隐藏 Anki 窗口")
        self.anki_conceal_check.setChecked(bool(self.config.get("anki_conceal", True)))
        self.add_wide(lay, self.anki_conceal_check,
                      "Anki 自己的界面会从屏幕、Alt+Tab 和任务栏缩略图里消失，\n"
                      "答题全在这个窗口里完成；退出背诵时它会回到原处（不激活）。\n"
                      "藏起来≠隐身，别把两个窗口摆在同一块屏幕上。")

        col.addStretch(1)

    def build_page_feed(self):
        col = self.new_page("社区")

        lay = self.new_card(col, "版式")
        self.feed_font_check = QCheckBox("社区页用单独字体")
        self.feed_font_check.setChecked(bool(self.config.get("feed_font_family")
                                             or self.config.get("feed_font_size")))
        self.feed_font_check.toggled.connect(self.on_feed_font_change)
        self.add_wide(lay, self.feed_font_check,
                      "社区统一流（Ctrl+Alt+F）的字体和字号默认和书页完全一样。\n"
                      "勾上就能单独调：社区流是长文档，一屏里信息多，\n"
                      "用比书页小一两号字更合适（书页的字号不受影响）。")

        feed_font_row = QWidget()
        feed_font_row.setObjectName("formRow")
        feed_font_layout = QHBoxLayout(feed_font_row)
        feed_font_layout.setContentsMargins(0, 0, 0, 0)
        feed_font_layout.setSpacing(8)
        self.feed_font_spin = QSpinBox()
        self.feed_font_spin.setRange(8, 60)
        self.feed_font_spin.setValue(int(self.config.get("feed_font_size")
                                         or self.config.get("font_size") or 14))
        self.feed_font_spin.valueChanged.connect(self.on_feed_font_change)
        self.feed_font_combo = QFontComboBox()
        self.feed_font_combo.setCurrentFont(QFont(self.config.get("feed_font_family")
                                                  or self.config.get("font_family", "Microsoft YaHei")))
        self.feed_font_combo.currentFontChanged.connect(self.on_feed_font_change)
        feed_font_layout.addWidget(QLabel("字号"))
        feed_font_layout.addWidget(self.feed_font_spin)
        feed_font_layout.addWidget(self.feed_font_combo, 1)
        self.feed_font_row = feed_font_row
        feed_font_row.setEnabled(self.feed_font_check.isChecked())
        self.add_row(lay, "字号 / 字体", feed_font_row)

        lay = self.new_card(col, "来源")
        self.source_checks = {}
        try:
            import feed_sources as _feed_sources
            choices = list(getattr(_feed_sources, "SOURCE_CHOICES", ()))
        except Exception:
            choices = []
        want_on = self.config.get("feed_sources", None)
        want_on = {str(x) for x in want_on} if isinstance(want_on, list) else None
        grid_host = QWidget()
        grid_host.setObjectName("formRow")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(6)
        for i, (key, label, _can) in enumerate(choices):
            cb = QCheckBox(label)
            cb.setChecked(key in want_on if want_on is not None else True)
            self.source_checks[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        self.add_wide(lay, grid_host,
                      "勾上的才在社区页里出现（[ ] 切换也只在勾上的之间轮）。\n"
                      "全不勾 = 只剩离线示例那一页（验版面用）。")

        lay = self.new_card(col, "登录")
        self.auth_rows = {}
        try:
            import feed_sources as _feed_sources
            _auth = _feed_sources.load_auth(_feed_sources.auth_path())
        except Exception:
            _auth = {}
        for key, label, can_auth in choices:
            if not can_auth:
                continue
            row = QWidget()
            row.setObjectName("formRow")
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(8)
            state = QLabel()
            state.setObjectName("stateLabel")
            btn = QPushButton("粘贴")
            btn.setObjectName("miniButton")
            btn.clicked.connect(lambda _c=False, k=key, lab=label: self.edit_cookie(k, lab))
            row_lay.addWidget(state, 1)
            row_lay.addWidget(btn)
            self.auth_rows[key] = state
            self.add_row(lay, label, row)
            self._refresh_auth_row(key)

        tools = QWidget()
        tools.setObjectName("formRow")
        tools_lay = QHBoxLayout(tools)
        tools_lay.setContentsMargins(0, 0, 0, 0)
        tools_lay.setSpacing(10)
        self.btn_scan_cookies = QPushButton("从浏览器读取")
        self.btn_scan_cookies.clicked.connect(self.scan_cookies)
        self.cookie_note = QLabel("只读本机浏览器里这几个站点的 Cookie")
        self.cookie_note.setObjectName("hintLabel")
        self.cookie_note.setWordWrap(True)
        tools_lay.addWidget(self.btn_scan_cookies)
        tools_lay.addWidget(self.cookie_note, 1)
        self.add_wide(lay, tools,
                      "读 Chrome / Edge / Brave / Vivaldi / Firefox 的 Cookie 库：**只读**、\n"
                      "只挑这几个站点的域名，找到就写进 feed_auth.json。\n"
                      "浏览器开着时那个库被它独占，读不了——会照实说；也可以先关掉浏览器再点。")

        lay = self.new_card(col, "显示")
        self.feed_lowkey_check = QCheckBox("低调显示")
        self.feed_lowkey_check.setChecked(bool(self.config.get("feed_lowkey", False)))
        self.add_wide(lay, self.feed_lowkey_check,
                      "工位上比老板键更不显眼的一道保险：只剩文字，字号配色和书页一致。")

        col.addStretch(1)

    # --- 登录（Cookie）：自动找 / 手动贴 ---------------------------------
    def _auth_data(self):
        try:
            import feed_sources as fs
            return fs.load_auth(fs.auth_path())
        except Exception:
            return {}

    def _refresh_auth_row(self, key):
        lab = getattr(self, "auth_rows", {}).get(key)
        if lab is None:
            return
        text = self.cookie_edits.get(key)
        if text is None:
            text = str((self._auth_data().get(key) or {}).get("cookie") or "")
        lab.setText("已配置" if str(text).strip() else "未配置")

    def edit_cookie(self, key, label):
        """手动贴一条 Cookie —— 浏览器锁着、或者它新版加密读不出来时的退路。"""
        cur = str((self._auth_data().get(key) or {}).get("cookie") or "")
        text, ok = QInputDialog.getText(self, "登录 %s" % label,
                                        "整行 Cookie（a=1; b=2，留空 = 清掉）：",
                                        QLineEdit.Normal, cur)
        if not ok:
            return
        self.cookie_edits[key] = text.strip()
        self._refresh_auth_row(key)

    def scan_cookies(self):
        """[从浏览器读取]：找一遍 → **只记下来**，点保存才落盘（取消就是没动过）。"""
        try:
            import feed_cookies
        except Exception as exc:
            QMessageBox.warning(self, "读不了浏览器", "这个模块没加载起来：%s" % exc)
            return
        found, notes = feed_cookies.scan()
        if not found:
            locked = list(dict.fromkeys(feed_cookies.locked_browsers()))
            if locked:
                names = "、".join(locked)
                answer = QMessageBox.question(
                    self, "要从浏览器读 Cookie",
                    "%s 正在运行，Cookie 库被它独占着，读不了。\n\n"
                    "现在关掉 %s 再读一次？（它下次打开会恢复标签页）" % (names, names),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer == QMessageBox.Yes:
                    for name in locked:
                        feed_cookies.close_browser(name)
                    for _i in range(15):            # 等它真的退出（最多 3 秒）
                        if not feed_cookies.locked_browsers():
                            break
                        time.sleep(0.2)
                    found, notes = feed_cookies.scan()
        if not found:
            QMessageBox.information(
                self, "没找到 Cookie",
                "没能从浏览器里读到可用的 Cookie。\n\n"
                + "\n".join(notes[:4] or ["这台机器上没找到浏览器 profile"])
                + "\n\n可以点某一行的「粘贴」，直接把浏览器里的 Cookie 贴进来。")
            self.cookie_note.setText(feed_cookies.describe(found, notes))
            return
        for key, info in found.items():
            self.cookie_edits[key] = info["cookie"]
            self._refresh_auth_row(key)
        self.cookie_note.setText(feed_cookies.describe(found, notes) + "（点保存生效）")
        log_event("登录：从浏览器读到 %s" % "、".join(sorted(found)))

    def save_cookie_edits(self):
        """把这一轮贴/找到的 Cookie 合进 feed_auth.json（其它键原样保留）。"""
        edits = dict(getattr(self, "cookie_edits", {}) or {})
        if not edits:
            return
        try:
            import feed_sources as fs
            path = fs.auth_path()
            data = fs.load_auth(path)
            for key, text in edits.items():
                text = str(text or "").strip()
                if text:
                    ent = data.get(key) if isinstance(data.get(key), dict) else {}
                    ent["cookie"] = text
                    data[key] = ent
                elif isinstance(data.get(key), dict):
                    data[key].pop("cookie", None)
                    if not data[key]:
                        data.pop(key, None)
            fs.save_auth(path, data)
            log_event("登录：写回 feed_auth.json（%d 个来源）" % len(edits))
        except Exception as exc:
            log_event("写 feed_auth.json 失败: %s" % exc)

    def build_page_ui(self):
        col = self.new_page("界面")

        lay = self.new_card(col, "元素")
        self.check_corners = QCheckBox("显示角标")
        self.check_corners.setChecked(bool(self.config.get("show_corners", True)))
        self.add_wide(lay, self.check_corners,
                      "自动挡时窗口左上、右下那两条短角线（用来和背景区分边界）。\n"
                      "关掉就是彻底干净的一页。")

        self.check_bar = QCheckBox("显示底部信息栏")
        self.check_bar.setChecked(bool(self.config.get("show_bar", True)))
        self.add_wide(lay, self.check_bar,
                      "窗口底部那一条状态行：卡片页是剩余数量和评分后果，社区页是当前第几条。\n"
                      "关掉之后页面只剩正文。")

        col.addStretch(1)

    def build_page_privacy(self):
        col = self.new_page("隐私")

        lay = self.new_card(col, "防护")
        self.check_antishot = QCheckBox("系统级防截屏")
        self.check_antishot.setChecked(self.config.get("antishot_mode", False))
        self.check_antishot.toggled.connect(self.on_antishot_toggled)
        self.add_wide(lay, self.check_antishot,
                      "开启后，肉眼可见，但截图/录屏时窗口会完全消失（透明）。\n"
                      "使用 Windows 系统底层保护。")

        lay = self.new_card(col, "安全文档")
        safe_row = QWidget()
        safe_row.setObjectName("formRow")
        safe_layout = QHBoxLayout(safe_row)
        safe_layout.setContentsMargins(0, 0, 0, 0)
        safe_layout.setSpacing(8)
        self.safe_doc_input = QLineEdit(self.config.get("safe_doc_path", ""))
        self.safe_doc_input.setPlaceholderText("留空 = 用内置的工作周报体例")
        btn_pick_safe = QPushButton("选择…")
        btn_pick_safe.clicked.connect(self.pick_safe_doc)
        safe_layout.addWidget(self.safe_doc_input, 1)
        safe_layout.addWidget(btn_pick_safe)
        self.add_row(lay, "文件", safe_row,
                     "可选：指定本机的一份文档（TXT / MD / EPUB）。\n"
                     "读不出来会自动回落内置版——任何情况下都不会露出书页。")

        col.addStretch(1)

    def build_page_source(self):
        col = self.new_page("数据源")

        lay = self.new_card(col, "Legado")
        self.ip_input = QLineEdit(self.config.get("ip"))
        self.add_row(lay, "地址", self.ip_input,
                     "本机 Legado 的地址，例如 http://127.0.0.1:1122。")

        col.addStretch(1)

    def on_auto_mode_toggled(self, checked):
        self.btn_bg_color.setEnabled(not checked)
        self.btn_text_color.setEnabled(not checked)
        self.opacity_slider.setEnabled(True)

    def on_antishot_toggled(self, checked):
        if self.main_window:
            hwnd = int(self.main_window.winId())
            set_window_protection(hwnd, checked)

    def on_opacity_change(self, value):
        new_opacity = value / 100.0
        self.config["opacity"] = new_opacity
        if self.main_window:
            self.main_window.apply_style()

    def on_font_change(self, _value=None):
        """字号 / 字体也是拖动即预览：以前只在「保存并应用」时才读，而那时又不重排，
        结果就是"改了字号没反应，翻一页才变"。现在和行距/页边距一样即时生效并落盘。"""
        self.config["font_size"] = self.font_spin.value()
        self.config["font_family"] = self.font_combo.currentFont().family()
        if self.main_window:
            self.main_window.apply_style()
            self.main_window.save_config()

    def on_typography_change(self, _value=None):
        """行距/段间距/页边距/首行缩进都是拖动即预览，并且**立刻写盘**。

        以前只改内存不落盘：改完不点「保存并应用」，一次崩溃/被杀/重启（比如我这边
        为了上新版重启一次）就会悄悄退回文件里旧的那个值——表现出来就是
        「页边距调整不生效」：调的时候看着对，下次打开又变回去了。"""
        self.config["line_spacing"] = self.spacing_spin.value()
        self.config["paragraph_spacing"] = self.para_spin.value()
        self.config["page_margin_chars"] = self.margin_spin.value()
        self.config["arrow_scroll_lines"] = self.arrow_spin.value()
        self.config["first_line_indent"] = self.indent_spin.value()
        self.config["paragraph_indent"] = self.para_indent_check.isChecked()
        if self.main_window:
            self.main_window.apply_style()
            self.main_window.save_config()

    def on_feed_font_change(self, _value=None):
        """社区页字体：勾上就写两个键，取消勾选就把键删掉（回到"跟着书页走"）。"""
        on = self.feed_font_check.isChecked()
        if on:
            self.config["feed_font_size"] = self.feed_font_spin.value()
            self.config["feed_font_family"] = self.feed_font_combo.currentFont().family()
        else:
            self.config.pop("feed_font_size", None)
            self.config.pop("feed_font_family", None)
        self.feed_font_row.setEnabled(on)
        if self.main_window:
            self.main_window.apply_style()      # 社区页正开着的话就地换字体
            self.main_window.save_config()

    def pick_text_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            self.temp_text_color = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"
            self.btn_text_color.setStyleSheet(f"background-color: {self.temp_text_color};")

    def pick_bg_color(self):
        color = QColorDialog.getColor(options=QColorDialog.ShowAlphaChannel)
        if color.isValid():
            self.temp_bg_color = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"
            self.btn_bg_color.setStyleSheet(f"background-color: {self.temp_bg_color};")

    def pick_anki_deck(self):
        """从正在运行的 Anki 里列出牌组（带今日待复习张数）供选择。

        Anki 没开 / 没装 AnkiConnect 时**照实说**，不静默给个空列表——
        否则人会以为"我没有牌组"，而其实是插件没连上。
        """
        if not self.main_window:
            return
        try:
            client = self.main_window.anki_client()
            client.ping()
            names = list(client.deck_names() or [])
            stats = client.deck_stats(names)
        except Exception as e:
            QMessageBox.warning(self, "读不到牌组", f"{type(e).__name__}\n\n{INSTALL_HINT}")
            return
        if not names:
            QMessageBox.information(self, "没有牌组", "Anki 里一个牌组都没有。")
            return
        labels = []
        for name in names:
            info = stats.get(name)
            if info is None:
                # 个别牌组（本机是「系统默认」）Anki 就是不给计数，照实说，别写"待复习 0"
                labels.append(f"{name}　（计数不可用）")
                continue
            due = int(info.get("new", 0)) + int(info.get("learn", 0)) + int(info.get("review", 0))
            labels.append(f"{name}　（待复习 {due}）")
        current = self.anki_deck_input.text().strip()
        index = names.index(current) if current in names else 0
        choice, ok = QInputDialog.getItem(self, "选择牌组", "隐形背诵用哪个牌组：",
                                          labels, index, False)
        if ok and choice:
            self.anki_deck_input.setText(names[labels.index(choice)])

    def test_anki_connection(self):
        """只在人主动点的时候连一次 Anki（打开设置不该被 Anki 拖住）。"""
        if not self.main_window:
            return
        try:
            client = self.main_window.anki_client()
            version = client.ping()
            cards = client.reviewed_today()
        except Exception as e:
            QMessageBox.warning(self, "连不上 AnkiConnect", f"{type(e).__name__}\n\n{INSTALL_HINT}")
            return
        QMessageBox.information(self, "连上了",
                               f"AnkiConnect 版本 {version}\n今日已复习 {cards} 张")

    def pick_safe_doc(self):
        """挑一份本机文档当"安全文档"（有人看屏幕时顶上）。"""
        options = QFileDialog.Options() | QFileDialog.DontUseNativeDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择安全文档", "",
            "文档 (*.txt *.md *.epub);;纯文本 (*.txt *.md);;EPUB (*.epub);;All Files (*)",
            options=options)
        if path:
            self.safe_doc_input.setText(path)

    def accept(self):
        self.config["ip"] = self.ip_input.text().strip()
        self.config["font_size"] = self.font_spin.value()
        self.config["font_family"] = self.font_combo.currentFont().family()
        self.config["line_spacing"] = self.spacing_spin.value()
        self.config["paragraph_spacing"] = self.para_spin.value()
        self.config["page_margin_chars"] = self.margin_spin.value()
        self.config["arrow_scroll_lines"] = self.arrow_spin.value()
        self.config["first_line_indent"] = self.indent_spin.value()
        self.config["paragraph_indent"] = self.para_indent_check.isChecked()
        if self.feed_font_check.isChecked():
            self.config["feed_font_size"] = self.feed_font_spin.value()
            self.config["feed_font_family"] = self.feed_font_combo.currentFont().family()
        else:
            self.config.pop("feed_font_size", None)
            self.config.pop("feed_font_family", None)
        self.config["boss_key"] = self.boss_key_input.text().strip()
        self.config["peek_key"] = self.peek_key_input.text().strip()
        self.config["safe_key"] = self.safe_key_input.text().strip()
        self.config["safe_doc_path"] = self.safe_doc_input.text().strip()
        self.config["anki_key"] = self.anki_key_input.text().strip() or "ctrl+alt+a"
        self.config["feed_key"] = self.feed_key_input.text().strip() or "ctrl+alt+f"
        lowkey_now = self.feed_lowkey_check.isChecked()
        _win = self.main_window
        if _win is not None:
            if lowkey_now != bool(self.config.get("feed_lowkey", False)):
                # 图要不要落地变了，手里那份得重取（缓存里存的是"要不要图"的答案）
                _win._feed_memo.clear()
            for _src in (getattr(_win, "feed_sources", None) or []):
                try:
                    _src.images_off = lowkey_now   # 当场生效，不用重启
                except Exception:
                    pass
        self.config["feed_lowkey"] = lowkey_now
        self.config["anki_deck"] = self.anki_deck_input.text().strip()
        self.config["anki_swallow_keys"] = self.anki_swallow_check.isChecked()
        self.config["anki_conceal"] = self.anki_conceal_check.isChecked()
        self.config["anki_api_key"] = self.anki_api_key_input.text().strip()
        self.config["text_color"] = self.temp_text_color
        self.config["bg_color"] = self.temp_bg_color
        self.config["ghost_mode"] = self.check_ghost_mode.isChecked()
        self.config["auto_mode"] = self.check_auto_mode.isChecked()
        self.config["antishot_mode"] = self.check_antishot.isChecked()
        self.config["show_corners"] = self.check_corners.isChecked()
        self.config["show_bar"] = self.check_bar.isChecked()
        if self.source_checks:
            # 只留勾上的键。一个都没勾就是空列表 = 全关（只剩离线示例那一页）
            self.config["feed_sources"] = [k for k, cb in self.source_checks.items()
                                           if cb.isChecked()]
        self.save_cookie_edits()
        if self.main_window:
            # 「保存并应用」= 真的生效、真的落盘（字体这类只在保存时才改的项也在里面）
            self.main_window.apply_style()
            self.main_window.save_config()
        super().accept()

    def reject(self):
        self.config["opacity"] = self.original_opacity
        # 排版的实时预览要退回去：点取消就该保持原样，不能偷偷留下预览值
        for key, value in self.original_typography.items():
            if value is None:
                self.config.pop(key, None)
            else:
                self.config[key] = value
        if self.main_window:
            self.main_window.apply_style()
            self.main_window.save_config()
            set_window_protection(int(self.main_window.winId()), self.config.get("antishot_mode", False))
        super().reject()


# ================= 主程序 =================
class StealthReader(QWidget):
    update_text_signal = pyqtSignal(str, bool)
    # 四个模式键（老板键/偷瞄/安全文档/背诵）都走全局热键注册（keyboard 库），窗口不在前台
    # 也能按；库的回调跑在它自己的分发线程上，所以一律发信号回主线程做事。
    hotkey_signal = pyqtSignal()
    peek_hotkey_signal = pyqtSignal()
    safe_hotkey_signal = pyqtSignal()
    review_hotkey_signal = pyqtSignal()
    feed_hotkey_signal = pyqtSignal()
    # 社区页取数一律在后台线程里跑，取回来用这个信号回主线程（payload 一律是普通 Python 对象）
    feed_data_signal = pyqtSignal(str, object)
    # 偷瞄模式的钩子回调只发信号（QueuedConnection），翻页和退出都回到主线程做
    peek_wheel_signal = pyqtSignal(int)
    peek_exit_signal = pyqtSignal(str)
    bookshelf_updated_signal = pyqtSignal(list)
    # 背诵模式的按键也来自钩子线程（那里不能碰界面），一律发信号回主线程处理
    review_key_signal = pyqtSignal(str)
    # 社区页的键也来自钩子线程（偷瞄时窗口不聚焦，只能在全局层收），发回主线程处理
    feed_key_signal = pyqtSignal(int)
    # 书页的翻页/滚动键同理：偷瞄里按空格、方向键不该被当成"别的输入"而退出偷瞄
    book_key_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.load_config()
        self.is_settings_open = False

        # --- 网络书架数据 ---
        self.books = []
        self.current_book = None
        self.current_chapter_index = 0
        self.current_toc = []

        # --- 本地书籍数据 ---
        self.is_local_mode = False  # 模式标记
        self.local_full_text = ""  # 本地文件全文内容
        self.local_start_index = 0  # 当前页起始字符在全文中的索引 (锚点)
        self.local_page_history = []  # 记录翻页历史，用于"上一页"
        self.local_file_path = ""  # 当前文件路径
        self.local_chapters = []  # 本地书目录 [(标题, 起始偏移, 层级)]；epub 和 txt 都会有
        self.local_heading_offsets = set()  # 标题行的字符偏移，首行缩进要跳过它们
        self.displaying_local_text = False  # 当前显示的文本是否来自本地书（防止标题表串到网络章节上）

        # --- 划线 / 书签 ---
        self.annotations = load_annotations()   # {书key: {"highlights": [...], "bookmarks": [...]}}
        self.highlight_default_color = "yellow"  # 上次用过的颜色即下次默认
        self.is_popup_open = False               # 弹菜单/对话框时别让幽灵模式把字淡掉
        self.ghost_veiled = False                # 幽灵态下连高亮一起藏，别让彩色条露馅
        self.filter_drag_origin = None           # 中键/Alt+左键拖动窗口用
        self.action_panel = None                 # 划线确认面板（同一时刻只有一个）
        self.pending_selection = None            # 待确认的划选 (start, end, text)

        # --- 偷瞄模式（Ctrl+E 浮现 + 全局滚轮翻页）---
        self.peek_mode = False
        self.peek_was_visible = False   # 进入偷瞄前窗口是否可见，退出时照原样恢复
        self.peek_last_toggle = 0.0     # 和老板键一样防连击
        self.peek_last_exit = 0.0       # 刚退出的一瞬，Ctrl+E 的余波不再当新指令
        self.peek_shortcut = None
        self.peek_input_hook = GlobalInputHook(self)

        # --- 安全文档模式（有人看屏幕时换成一份预置的工作文档）---
        self.safe_mode = False
        self.safe_snapshot = None       # 进模式前的书状态，退出时按它恢复
        self.safe_shortcut = None
        self.review_shortcut = None

        # --- 隐形背诵模式（Anki 卡片当文档看）---
        self.review_mode = False
        self.review_session = None      # 牌组 / 当前卡 / 是否翻面 / 本次已背 / 上次提示
        self.review_snapshot = None     # 进背诵前的书状态（和安全文档共用同一套快照函数）
        self.review_client = None       # anki_client.AnkiClient，惰性创建
        self.review_busy_until = 0.0    # Anki 忙时的退避到什么时候
        self.review_entering = False    # 正在后台打开牌组（这期间再按热键不重复进）
        self.review_media_dir = ""      # collection.media：进牌组时取一次就缓存（每次取要 60ms）
        self.review_poll_tick = 0       # 对账节拍：计数不用每拍都拉
        # AnkiConnect 一律在这个后台线程里跑（每个 gui* 调用固定 ~60ms，不能放主线程）
        self.anki_worker = None
        self.anki_tasks = {}            # task_id -> {ok, err, key}
        self.anki_inflight = set()      # 排队/执行中的任务 key（去重：对账不许堆队）
        self.anki_task_seq = 0

        # --- 社区统一流（知乎/V2EX/微博…统一成一种帖子，当第三个内容页）---
        self.feed_mode = False
        self.feed_snapshot = None       # 进社区页前的书状态（和安全文档/背诵共用同一套快照函数）
        self.feed_shortcut = None
        self.feed_nav = None            # feed.FeedNav：选中/翻页/进详情/折叠全在它里面
        self.feed_sources = []          # 已登记的来源适配器（批次 B 起每加一个站就在这里多一个）
        self.feed_source_i = 0
        self.feed_busy = False          # 后台取数中（同一次取数不重复发）
        self.feed_more_armed = True     # 到底自动续页：一次到底只触发一次
        self.feed_pending = None        # 忙着的时候来的取数请求，等回来补上（见 feed_load）
        self.feed_board_pending = False  # 正在拉板块表（回来要弹选择器，见 feed_load_boards）
        self.feed_boards_tried = set()   # 拉过全量板块表的来源（拉不到也别反复去拉）
        self.feed_detail_top = False     # 刚点进帖子：正文回来时把视图钉在顶部
        self.feed_switch_prev = None     # 上次切社区/切板块之前的样子（Z 撤回用）
        self.feed_list_pos = {}          # (来源, 板块) -> 列表滚到哪了（本次会话内有效）
        self.feed_restore_pos = None     # 下一次重画之后要落到的位置（None = 保持原样）
        self._feed_pending_scroll = None  # 已经排上队、还没执行的那次恢复（可以被取消）
        self._feed_press_had_sel = False  # 按下那一刻是不是正选着字（选字就别折叠/别进帖）
        self.feed_gen = 0               # "视图代次"：换代后回来的旧结果一律丢掉（见 on_feed_data）
        # 内存里留一份刚取到的内容：切栏目/退回列表/再进同一个帖子时**先把它画上去**，
        # 后台照旧去取（磁盘缓存 TTL 一到就得重新请求，小红书那一页要 1.7 秒），
        # 取回来的内容如果一模一样就不再重排——这条同时治好了"刷新一下整页抖一下、滚动条归位"。
        self._feed_memo = {}
        self._feed_sig = None           # 上次真正画出来的内容签名（见 _feed_sig_now）
        self._feed_inflight = {}        # 正在取的 key -> Event：后来的等前面那个，不重复发请求
        self.FEED_MEMO_TTL = 90         # 内存里这份多久算"还热"（秒）：热就不发请求，"刷新"照样真取
        self._feed_warm_post = None     # 光标底下那条（停稳了就去预取它）
        self._feed_warm_timer = QTimer(self)
        self._feed_warm_timer.setSingleShot(True)
        self._feed_warm_timer.setInterval(150)
        self._feed_warm_timer.timeout.connect(self._feed_warm_now)
        # 文字直播：详情页是一条"进行中"的比赛就每 20 秒静默再问一次（人不用一直按 R）。
        # 只在**详情页且比赛没结束**时开着：别让它在列表页或已结束的比赛上白跑请求。
        self.feed_live_timer = QTimer(self)
        self.feed_live_timer.setInterval(5000)      # 真人要的"每 5 秒刷一次"
        self.feed_live_timer.timeout.connect(self.feed_live_tick)
        # 5 秒一拍这么密，失败必须自己退让（见 feed_live_tick / on_feed_data）
        self._feed_live_fail = 0                     # 连着失败了几拍
        self._feed_live_retry_at = 0.0               # 退让到什么时候之前不发请求
        if FEED_OK:
            try:
                self.feed_sources = feed_mod.build_sources(
                    os.path.join(RES_DIR, "feed_demo"),
                    cache_root=os.path.join(APP_DIR, "cache", "feed"),
                    config=self.config, log=log_event)
            except Exception as _e:
                log_event(f"社区源登记失败: {_e}")
            self.feed_nav = feed_mod.FeedNav()
            # 上次停在哪个来源就回到哪个（配置里记的是 key；找不到就回默认的混流）
            want = str(self.config.get("feed_source") or "")
            if want:
                for _i, _s in enumerate(self.feed_sources):
                    if getattr(_s, "key", "") == want:
                        self.feed_source_i = _i
                        break

        # 老板键看门狗：keyboard 库的监听线程偶尔会静默死亡（锁屏/睡眠/钩子被系统强拆），
        # 每 45 秒查一次，死了就重新注册
        self.hotkey_watchdog = QTimer(self)
        self.hotkey_watchdog.setInterval(45000)
        self.hotkey_watchdog.timeout.connect(self.check_hotkeys_alive)
        self.hotkey_watchdog.start()
        self.hotkey_reregister_tick = 0   # 看门狗每两拍静默重挂一次，理由见 check_hotkeys_alive

        # --- 界面控制 ---
        self.single_line_height = 20
        self.is_mouse_in = False
        self.is_resizing = False
        self.is_moving = False
        self.resize_margin = 15
        self._last_resize_repage = 0.0
        self.last_toggle_time = 0
        self.local_shortcut = None
        self.book_selector_dialog = None
        self.oldPos = QPoint(0, 0)

        self.chameleon_timer = QTimer(self)
        self.chameleon_timer.setInterval(500)
        self.chameleon_timer.timeout.connect(self.adjust_color_to_background)

        # 视线滚到哪、进度就记到哪：滚轮会连续触发，攒一会儿再落盘
        self.progress_save_timer = QTimer(self)
        self.progress_save_timer.setSingleShot(True)
        self.progress_save_timer.setInterval(1200)
        self.progress_save_timer.timeout.connect(self.save_config)

        # 背诵时和 Anki 对账：人可能跑去 Anki 自己的窗口答题，窗口可见时每 2.5 秒核一次
        self.review_timer = QTimer(self)
        self.review_timer.setInterval(2500)
        self.review_timer.timeout.connect(self.review_tick)

        self.initUI()
        self.initTray()

        self.update_text_signal.connect(self.on_update_text_safe)
        # 热键"同一下只认第一次"的标记（见 hotkey_is_repeat）：tag -> 这一下按的键名，
        # 松键回调负责清。没有它，按住老板键不放会被系统自动重复连点成"显示-隐藏-显示"。
        self._hotkey_gate = {}
        self._hotkey_gate_at = {}
        self.hotkey_signal.connect(lambda: self.toggle_window("hotkey"))
        self.peek_hotkey_signal.connect(self.toggle_peek)
        self.safe_hotkey_signal.connect(self.toggle_safe_mode)
        self.review_hotkey_signal.connect(self.toggle_review_mode)
        self.feed_hotkey_signal.connect(self.toggle_feed_mode)
        self.feed_data_signal.connect(self.on_feed_data, Qt.QueuedConnection)
        self.peek_wheel_signal.connect(self.on_peek_wheel, Qt.QueuedConnection)
        self.peek_exit_signal.connect(self.on_peek_exit, Qt.QueuedConnection)
        self.review_key_signal.connect(self.on_review_key, Qt.QueuedConnection)
        self.feed_key_signal.connect(self.on_feed_peek_key, Qt.QueuedConnection)
        self.book_key_signal.connect(self.on_peek_book_key, Qt.QueuedConnection)
        self.bookshelf_updated_signal.connect(self.on_bookshelf_updated)
        # 最后一道保险：任何退出路径（托盘退出、右键退出、关窗、异常退出）都要停掉 Anki
        # 后台线程。QThread 还在跑就被析构，PyQt 会直接崩，而且 Anki 可能停在"藏起来"的状态。
        try:
            QApplication.instance().aboutToQuit.connect(self.stop_anki_worker)
        except Exception:
            pass

        self.refresh_hotkeys()
        self.register_session_notification()   # 锁屏解锁后靠它主动重挂热键
        # 上次进程要是被强杀（任务管理器/崩溃），Anki 可能还停在我们藏起来的状态：延迟一会儿体检一次
        QTimer.singleShot(1500, self.recover_anki_window)

        # 尝试恢复上次打开的本地文件
        if self.config.get("last_local_file") and os.path.exists(self.config["last_local_file"]):
            self.update_text_signal.emit("正在恢复上次阅读...", False)
            QTimer.singleShot(500, self.restore_last_local_file)
        elif self.config["ip"] and self.config["ip"].startswith("http"):
            self.fetch_bookshelf_silent()
            self.update_text_signal.emit("初始化完成。\n右键菜单可打开本地TXT文件。", False)
        else:
            self.update_text_signal.emit("欢迎使用。\n右键打开本地书籍或设置Legado。", False)

        if self.config.get("antishot_mode", False):
            QTimer.singleShot(100, lambda: set_window_protection(int(self.winId()), True))

    def restore_last_local_file(self):
        path = self.config["last_local_file"]
        pos = self.config.get("last_local_pos", 0)
        self.load_local_file(path, target_pos=pos)

    # --- 打开本地文件 (防止 0xC0000409 崩溃) ---
    def open_local_file_dialog(self):
        if self.review_mode:
            # 要换书了：先把背诵退掉，免得卡片和书混在一起
            self.exit_review_mode(reason="open_book")
        options = QFileDialog.Options()
        # 【关键】禁用 Windows 原生对话框，改用 Qt 内置对话框
        options |= QFileDialog.DontUseNativeDialog

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择书籍文件",
            "",
            "书籍 (*.epub *.txt);;EPUB (*.epub);;纯文本 (*.txt);;All Files (*)",
            options=options
        )

        if file_path:
            # 检查是否是同一本书
            last_file = self.config.get("last_local_file", "")

            is_same_file = False
            if last_file:
                try:
                    is_same_file = os.path.normpath(file_path) == os.path.normpath(last_file)
                except:
                    is_same_file = (file_path == last_file)

            if is_same_file:
                # 是同一本书：恢复上次进度
                saved_pos = self.config.get("last_local_pos", 0)
                self.load_local_file(file_path, target_pos=saved_pos)
            else:
                # 是新书：从头开始
                self.load_local_file(file_path, target_pos=0)

    def load_local_file(self, file_path, target_pos=0):
        try:
            content = ""
            chapters = []
            headings = set()
            if file_path.lower().endswith(".epub"):
                # epub：转成纯文本 + 章节目录 + 标题行（标题用来跳过首行缩进）
                try:
                    content, chapters, headings = load_epub_with_headings(file_path)
                except zipfile.BadZipFile:
                    self.update_text_signal.emit("EPUB 打不开（文件损坏或不是 epub）", False)
                    return
                except Exception as e:
                    self.update_text_signal.emit(f"EPUB 解析失败: {e}", False)
                    return
                if not content:
                    self.update_text_signal.emit("EPUB 没有可读文字", False)
                    return
            else:
                # 尝试多种编码读取
                try:
                    with open(file_path, 'r', encoding='utf-8-sig') as f:
                        content = f.read()
                except UnicodeDecodeError:
                    try:
                        with open(file_path, 'r', encoding='gb18030') as f:
                            content = f.read()
                    except Exception as e:
                        self.update_text_signal.emit(f"编码无法识别，请转为UTF-8或GBK", False)
                        return
                # 纯文本没有结构信息，只能按字面规则猜标题行（顺便当成目录用）
                headings = detect_txt_headings(content)
                chapters = txt_chapters_from_headings(content, headings)

            if not content:
                self.update_text_signal.emit("文件为空", False)
                return

            self.is_local_mode = True
            self.local_file_path = file_path
            self.local_full_text = content
            self.local_chapters = chapters
            self.local_heading_offsets = headings

            # 安全校验索引
            safe_pos = min(max(0, target_pos), len(content) - 1)
            self.local_start_index = safe_pos
            self.local_page_history = []

            # 【关键】加载时立即保存配置
            self.config["last_local_file"] = file_path
            self.config["last_local_pos"] = safe_pos
            self.save_config()

            # 划线/书签的偏移按原文校正一次（书换了文件时靠它救回来）
            self.validate_annotations()

            self.render_local_page()

            if safe_pos > 0:
                # 不再往书页上打"已恢复进度"消息——那会覆盖正文，让下一次翻页的几何测量
                # 把消息长度当成页宽，每次恢复后都固定偏移同样多的字
                log_event(f"恢复阅读进度: {os.path.basename(file_path)} @ {safe_pos}")

        except Exception as e:
            traceback.print_exc()
            self.update_text_signal.emit(f"打开文件失败: {str(e)}", False)

    # --- 排版：行距 / 段间距 / 页边距（统一切口） ---
    def line_spacing_percent(self):
        return clamp_config_int(self.config, "line_spacing", LINE_SPACING_DEFAULT,
                                LINE_SPACING_MIN, LINE_SPACING_MAX)

    def paragraph_spacing_percent(self):
        return clamp_config_int(self.config, "paragraph_spacing", 0,
                                PARA_SPACING_MIN, PARA_SPACING_MAX)

    def paragraph_spacing_px(self):
        """段间距按行高换算：行高多少，就按百分比给多少空白。"""
        line_height = QFontMetrics(self.text_edit.font()).lineSpacing()
        return int(line_height * self.paragraph_spacing_percent() / 100)

    def char_width(self):
        """1 字 = 一个全角空格宽（换字号、换字体跟着变）。"""
        return QFontMetrics(self.text_edit.font()).horizontalAdvance("\u3000")

    def page_margin_chars(self):
        """页边距，单位"字"：四边同一个值，字号/字体一变就跟着等比变，不会越看越细。

        老配置里 page_margin 存的是像素（载入时 load_config 会留个换算标记），
        这里拿到当前字体后换算一次并固化，之后就只认 page_margin_chars 了。"""
        legacy_px = self.config.pop("_page_margin_legacy_px", None)
        if legacy_px is not None:
            try:
                px = float(legacy_px)
            except (TypeError, ValueError):
                px = 0.0
            self.config["page_margin_chars"] = round(max(0.0, px) / max(1, self.char_width()), 2)
            self.config.pop("page_margin", None)
        return clamp_config_number(self.config, "page_margin_chars", PAGE_MARGIN_DEFAULT,
                                   PAGE_MARGIN_MIN, PAGE_MARGIN_MAX)

    def _book_font(self):
        """阅读页该用的字体：**只看配置**，不看控件此刻是什么（那可能刚被别的页换过）。"""
        return QFont(self.config.get("font_family", "Microsoft YaHei"),
                     int(self.config.get("font_size", 14)))

    def _pin_book_font(self, tag=""):
        """把书页字体按阅读设置钉死：控件字体、文档默认字体、**每个字符的 charFormat**。

        三层里最狠的是第三层：字符自己带的字体优先级最高，只钉前两层的话屏幕上照样是
        上一页那套字。真人报过"进社区页点个帖子、退出之后阅读字体变成帖子那个"，
        而 `document().defaultFont()` 这时读出来是对的——所以以前那些只查默认字体的
        断言全绿，人也确实看见了不一样的字体。
        只在**真的钉回来过**（原本某一层和配置不符）时才记一行日志，平时不刷屏。
        """
        font = self._book_font()
        doc = self.text_edit.document()
        want = (font.family(), float(font.pointSizeF()))
        had_ctrl = (self.text_edit.font().family(), float(self.text_edit.font().pointSizeF()))
        had_doc = (doc.defaultFont().family(), float(doc.defaultFont().pointSizeF()))
        self.text_edit.setFont(font)
        doc.setDefaultFont(QFont(font))
        cur = QTextCursor(doc)
        cur.select(QTextCursor.Document)
        fmt = QTextCharFormat()
        fmt.setFontFamily(font.family())
        fmt.setFontPointSize(float(font.pointSizeF()))
        cur.mergeCharFormat(fmt)          # 合并：只改字体，别的字符属性（划线颜色之类）不动
        self.text_edit.moveCursor(QTextCursor.Start)
        if tag and (had_ctrl != want or had_doc != want):
            log_event("书页字体被带跑过，已按阅读设置钉回来（%s：控件 %s/%.0f、文档 %s/%.0f → %s/%.0f）"
                      % (tag, had_ctrl[0], had_ctrl[1], had_doc[0], had_doc[1],
                         want[0], want[1]))

    def page_margin_px(self):
        return int(round(self.char_width() * self.page_margin_chars()))

    def first_line_indent_chars(self):
        return clamp_config_number(self.config, "first_line_indent", FIRST_LINE_INDENT_DEFAULT,
                                   FIRST_LINE_INDENT_MIN, FIRST_LINE_INDENT_MAX)

    def paragraph_indent(self):
        """界面上那个「段落缩进」开关。"""
        return bool(self.config.get("paragraph_indent", True))

    def heading_no_indent(self):
        """标题不缩进是自动行为；这个键只留给 txt 猜错标题时手动关掉。"""
        return bool(self.config.get("heading_no_indent", True))

    def first_line_indent_px(self):
        """1 字符 = 一个全角空格的宽度，换字号字体时会跟着变。"""
        if not self.paragraph_indent() or self.first_line_indent_chars() <= 0:
            return 0
        return int(round(self.char_width() * self.first_line_indent_chars()))

    def skip_first_line_indent(self):
        """挑出不该缩进的块：标题行，以及页首落在段落中间时的续行。

        正文是一整串纯文本，所以"这段是不是标题"只能靠字符偏移查表；
        "这块是不是段落开头"则看它前一个字符是不是换行——翻页是按像素切的，
        一页的第一块经常是上一段的后半截，给它加缩进会像是新起了一段。"""
        if self.first_line_indent_px() <= 0:
            return
        local = self.displaying_local_text
        start = self.local_start_index if local else 0
        source = self.local_full_text if local else None
        headings = self.local_heading_offsets if (local and self.heading_no_indent()) else set()

        no_indent = QTextBlockFormat()
        no_indent.setTextIndent(0)
        block = self.text_edit.document().begin()
        while block.isValid():
            absolute = start + block.position()
            if absolute in headings:
                QTextCursor(block).mergeBlockFormat(no_indent)
            elif source is not None and absolute > 0 and source[absolute - 1:absolute] != "\n":
                QTextCursor(block).mergeBlockFormat(no_indent)
            block = block.next()

    def apply_text_format(self):
        """行距/段间距/页边距/首行缩进：Qt 样式表里没有这些属性，只能写进文本格式和文档本身。
        每次换文本都要重新套一遍，否则翻页探测会按上一套排版测量。"""
        document = self.text_edit.document()

        margin = self.page_margin_px()
        # 页边距一律用视口留白（viewportMargins）：文档留白会跟着内容滚走，
        # 偷瞄/滚轮一滚顶部就没边距了；视口留白是固定的框，滚动也吃不掉。
        if document.documentMargin() != 0:
            document.setDocumentMargin(0)

        block_format = QTextBlockFormat()
        block_format.setLineHeight(self.line_spacing_percent(), QTextBlockFormat.ProportionalHeight)
        block_format.setTopMargin(self.paragraph_spacing_px())
        block_format.setTextIndent(self.first_line_indent_px())
        # 两端对齐：中文排版本来就该这样。右边缘从"参差"变成一条线，顺带把"放不下一个整字"
        # 剩下的那几像素（视口宽 mod 字宽）摊进字间，右留白才和左留白对称。
        # 段末短行不受影响（Qt 不拉伸段落的最后一行），标题是单行块，同样不会被拉伸。
        block_format.setAlignment(Qt.AlignJustify)

        cursor = QTextCursor(document)
        cursor.select(QTextCursor.Document)
        cursor.mergeBlockFormat(block_format)

        # 第一段不留上边距，否则每页顶部会凭空空出一块
        if block_format.topMargin() > 0:
            no_top_margin = QTextBlockFormat()
            no_top_margin.setTopMargin(0)
            QTextCursor(document.firstBlock()).mergeBlockFormat(no_top_margin)

        # 标题与页首续行不缩进，必须在整体套用之后再逐块修正
        self.skip_first_line_indent()

        # 排版定稿后重画划线（换页/改字号/换样式都会走到这里）
        self.apply_highlight_selections()

        # 页边距：四边固定留白 + 底部按"最后一行完整"再收一次
        self.apply_page_margins()

    # --- 页边距（四边留白） ---
    def page_cut_line(self, limit):
        """本页正文的收边：返回 (最后一个完整行的行盒下沿, 这一行的行内行距)。

        行盒下沿有两个身份：它是"下一页从哪继续"的分界（翻页探测必须用它，探到更靠上的
        位置会落回本页最后一行，翻页就重复一行），也是裁切的基准线。
        行盒下沿比这一行真正的墨迹还低 `lead` 像素（行距在行盒里留下的空隙），裁切时把这
        `lead` 补回给裁切量，底部"看得见的留白"才不会白多出几像素。
        判"这一行算不算完整"用的是墨迹下沿（行盒下沿 − lead）：留白可以小到刚好等于设定值，
        不必为几像素的空隙把整整一行推给下一页。"""
        doc = self.text_edit.document()
        layout = doc.documentLayout()
        cut = None
        lead = 0
        block = doc.begin()
        while block.isValid():
            bl = block.layout()
            top = layout.blockBoundingRect(block).top()
            if top > limit:
                break
            if bl is not None:
                for i in range(bl.lineCount()):
                    line = bl.lineAt(i)
                    r = line.rect()
                    if top + r.bottom() - line.leading() <= limit:
                        cut = top + r.bottom()
                        lead = line.leading()
                    else:
                        # 这一行越过底线：整行留给下一页
                        return (cut if cut is not None else top + r.top()), lead
            block = block.next()
        return (cut if cut is not None else limit), lead

    def apply_page_margins(self, cut=True):
        """页边距（四边）：左右和顶部是固定的视口留白，底部按"本页最后一行完整"再收一次。

        分成两层是有原因的：
          - 固定框（viewportMargins 的上下左右）：滚动吃不掉，所以偷瞄用滚轮平滑滚动时
            顶部那条留白照样在（文档留白会被内容滚过去，看着就是"顶部没有页边距"）；
          - 底部再按行位置收边：正文是按几何切页的，切点落在哪一行就显示到哪，
            压在窗口下沿的那一行会被切掉半行，所以要退到最后一个完整行的下沿。
        四边量到的是同一个东西——正文墨迹：顶部/左右是固定留白，底部再多补回最后一行行盒
        里那几像素行距（见 page_cut_line），于是"看得见的底部留白 ≥ 设定值"且最小就是设定值。

        `cut=False` 给**卡片页**用：卡片是可以滚动的（长答案超过一屏），底边出现半行是正常的
        滚动现象；书页那种"退到最后一个完整行"的裁切反而会害人——实测一张 130px 高的短卡片
        被算出 261px 的下边距，视口只剩卡片那么高，窗口一缩小视口直接掉到 0。"""
        te = self.text_edit
        vm = te.viewportMargins()
        margin = self.page_margin_px()
        if te.verticalScrollBar().value() != 0:
            return   # 行矩形是文档坐标，滚动过就不是视口坐标了（翻页时会重算）
        if margin <= 0:
            # 页边距为 0：不裁边，页底就是视口下沿（完全回到没有页边距时的行为）
            self.page_content_bottom = te.viewport().height()
            if vm.left() or vm.top() or vm.right() or vm.bottom():
                te.setViewportMargins(0, 0, 0, 0)
            return
        if not cut:
            self.page_content_bottom = te.viewport().height() + vm.bottom() - margin
            if (vm.left(), vm.top(), vm.right(), vm.bottom()) != (margin, margin, margin, margin):
                te.setViewportMargins(margin, margin, margin, margin)
            return
        base_h = te.viewport().height() + vm.bottom()   # 未按页裁切时的可见高度
        if base_h <= margin * 3:
            # 窗口被压到只剩一两行：别留着上一次算出的裁切量把正文区压没
            self.page_content_bottom = base_h
            bottom = max(0, base_h - 1) if vm.bottom() >= base_h else vm.bottom()
            if (vm.left(), vm.top(), vm.right(), vm.bottom()) != (margin, margin, margin, bottom):
                te.setViewportMargins(margin, margin, margin, bottom)
            return
        cut, lead = self.page_cut_line(base_h - margin)
        # 页底裁到"最后一个完整行的墨迹下沿"（行盒下沿补掉 lead），底部看得见的留白才到位
        self.page_content_bottom = cut
        bottom = int(max(0, min(base_h - 1, base_h - cut + lead)))
        if (vm.left(), vm.top(), vm.right(), vm.bottom()) != (margin, margin, margin, bottom):
            te.setViewportMargins(margin, margin, margin, bottom)

    def set_display_text(self, text):
        """换文本的唯一入口：换完立刻套同一套排版，否则翻页会按旧排版测量。

        铺字前后都把书页字体钉一遍（`_pin_book_font`）：社区页/卡片页会把控件字体、
        文档默认字体、甚至字符自带的 charFormat 里的某几层换掉。**字符上的 charFormat
        优先级最高**——只钉前两层的话，屏幕上照样是上一页那套字（真人报的"退出社区页后
        正文变成帖子那个字体"就是这么来的，而且光看 `document().defaultFont()` 还查不出来）。
        """
        self._pin_book_font("铺字前")
        # 字符格式已在控件自己的 setPlainText 里清掉（见那里的说明），这里只管钉字体。
        self.text_edit.setPlainText(text)
        self._pin_book_font("铺字后")
        self.apply_text_format()

    # --- 本地分页渲染算法 (锚点核心) ---
    def render_local_page(self):
        if self.feed_mode:
            # 社区页是一份 HTML 文档（滚动加载），绝不能拿书页那套"切 5000 字缓冲区"来铺：
            # 滚轮、缩放、偷瞄都会拐到这里，一旦铺下去，社区页当场被书页顶掉。
            return
        if not self.is_local_mode or not self.local_full_text:
            return

        # 截取缓冲区（保证填满屏幕，取5000字足以覆盖各种屏幕）
        buffer_length = 5000
        end_buffer = min(self.local_start_index + buffer_length, len(self.local_full_text))

        display_text = self.local_full_text[self.local_start_index: end_buffer]

        self.displaying_local_text = True
        self.set_display_text(display_text)

        # 【关键】强制滚动条回顶，确保 local_start_index 对应的字符永远在第一行
        self.text_edit.verticalScrollBar().setValue(0)

        # 回顶之后再算页底裁切线：这时文档坐标和视口坐标才是一套
        self.apply_page_margins()

        # 改视口留白时 Qt 有可能顺手调一下滚动位置，再钉一次页首
        self.text_edit.verticalScrollBar().setValue(0)

    def displayed_is_local_page(self):
        """正文区里现在显示的，是不是 local_full_text 从 local_start_index 起的那一段。

        进/出安全文档、切网络书的**那一瞬间**，正文区还是上一份内容，而 local_* 已经换成
        新的了。这时按几何位置算出来的"锚点"是另一份文档里的坐标——拿它当页首就会把进度
        改坏（实测：翻到 27137 后进安全文档，last_local_pos 被写成 72；在安全文档里按老板
        键唤出一次，又被写成 0，等于把读到哪儿整段清掉）。"""
        if not (self.is_local_mode and self.local_full_text):
            return False
        shown = self.text_edit.toPlainText()
        if not shown:
            return False
        want = self.local_full_text[self.local_start_index: self.local_start_index + 24]
        n = min(24, len(shown), len(want))
        return n > 0 and shown[:n] == want[:n]

    def current_top_offset(self):
        """当前视口顶那一行第一个字，在全文里的偏移。

        没滚动过时它就是本页页首（= local_start_index）；用滚轮/偷瞄滚过之后，
        视口顶已经不是页首了——重排（改窗口大小、改字号）要认住的是**这一行**。"""
        if not (self.is_local_mode and self.local_full_text):
            return self.local_start_index
        if not self.displayed_is_local_page():
            return self.local_start_index   # 屏幕上是上一份内容，几何位置不算数
        pos = self.text_edit.cursorForPosition(QPoint(0, 0)).position()
        return max(0, min(self.local_start_index + pos, len(self.local_full_text) - 1))

    def repage_to(self, anchor):
        """把锚点（某个字符）放到页首重新铺一页。

        改窗口大小 / 改字号之后如果还从原来的 local_start_index 铺，人就被甩回本页最上面
        （看着就是"阅读进度老是跳回"）。把锚点当成新的页首，视线落点不变，
        顺带把 last_local_pos 也更新过去——重启后恢复的就是你正在看的那一行。"""
        if not (self.is_local_mode and self.local_full_text):
            return
        if not self.displayed_is_local_page():
            # 切文档的中间态：锚点属于旧文档，一个字都不许改，只按当前 local_start_index 重铺
            # （安全文档铺它自己的第 0 字，书铺快照里的原位）
            self.render_local_page()
            return
        anchor = max(0, min(int(anchor), len(self.local_full_text) - 1))
        self.local_start_index = anchor
        if not self._book_veiled():
            # 安全文档 / 背诵模式下 local_* 不是书的坐标，绝不能写进真书的进度
            self.config["last_local_pos"] = anchor
        self.render_local_page()

    def record_view_position(self, *_):
        """视线滚到哪，阅读进度就记到哪。

        本地阅读有两种滚动：翻页会推进 local_start_index，而**页内滚轮 / 偷瞄滚轮只动视口**，
        local_start_index 原地不动，要滚满 5000 字缓冲区才更新一次。只记 local_start_index
        的话，在偷瞄里滚了一屏又一屏，存档还停在缓冲区起点——重启/重新打开就"上翻"回那一行
        （实测：人在 31819 划线，存档停在 29161，差 2700 字）。这里跟着视口顶走，并延迟落盘
        （滚轮连续触发，不必每个 notch 都写一次文件）。安全文档模式下正文不是书，一个字节不记。"""
        if self._book_veiled() or not (self.is_local_mode and self.local_full_text):
            return
        if not self.displayed_is_local_page():
            return
        self.config["last_local_pos"] = self.current_top_offset()
        self.progress_save_timer.start()

    # --- 核心：基于几何坐标探测下一页起始位置 ---
    def calc_next_page_start(self):
        """利用视图几何坐标，探测屏幕底部边缘的字符位置"""
        # 【新增保护】防止空内容计算
        if not self.text_edit.toPlainText():
            return 0

        viewport_h = self.text_edit.viewport().height()
        # 探测点：正文下沿（最后一个完整行的行盒下沿）再往下一点点 = 下一行的开头。
        # 不能拿视口下沿来探：为了把看得见的底部留白收到设定值，视口下沿会比行盒下沿更高
        # （落在最后一行行盒内部），探到的会是这一行本身，翻页就重复一行。
        base_y = max(viewport_h, getattr(self, "page_content_bottom", viewport_h))
        target_y = int(base_y) + 2

        cursor = self.text_edit.cursorForPosition(QPoint(0, target_y))
        next_pos_in_buffer = cursor.position()

        # 异常处理：如果一页装不满，cursor会指向文档末尾
        if next_pos_in_buffer >= len(self.text_edit.toPlainText()):
            return len(self.text_edit.toPlainText())

        return self.with_page_overlap(next_pos_in_buffer)

    def with_page_overlap(self, next_pos):
        """把下一页的起点往前挪几行，让上一页末尾的几行在新一页顶部重现。

        紧接上一页最后一行开始，视线等于直接落进陌生段落的开头，要往回找才能接上；
        退 PAGE_OVERLAP_LINES 行之后，重复的那几行本身就是接续位置的标记。

        三种情况退回原值，宁可少重叠也不能把翻页卡住：
        页首就在缓冲区开头（退无可退）、字体或窗口小到重叠超过半页、
        Qt 没能退动。
        """
        lines = clamp_config_int(self.config, "page_overlap_lines",
                                 PAGE_OVERLAP_LINES, 0, PAGE_OVERLAP_MAX)
        if lines <= 0 or next_pos <= 0:
            return next_pos

        cursor = self.text_edit.textCursor()
        cursor.setPosition(next_pos)
        cursor.movePosition(QTextCursor.Up, QTextCursor.MoveAnchor, lines)
        start = cursor.position()

        if start <= 0 or start >= next_pos:
            return next_pos
        if next_pos - start > next_pos // 2:
            return next_pos
        return start

    # --- 核心：基于反向排版探测上一页起始位置 ---
    def calc_prev_page_start(self):
        """通过加载前文并滚到底部，探测上一页的起始位置"""
        if self.local_start_index == 0:
            return 0

        self.text_edit.setUpdatesEnabled(False)
        try:
            buffer_size = 5000
            temp_start = max(0, self.local_start_index - buffer_size)
            prev_content = self.local_full_text[temp_start: self.local_start_index]

            self.displaying_local_text = True
            self.set_display_text(prev_content)

            scrollbar = self.text_edit.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

            cursor = self.text_edit.cursorForPosition(QPoint(0, 0))
            chars_in_prev_page = len(prev_content) - cursor.position()

            real_prev_start = self.local_start_index - chars_in_prev_page

            return max(0, real_prev_start)
        finally:
            self.text_edit.setUpdatesEnabled(True)

    # --- 翻页逻辑 (即时存档 + 几何分页) ---
    def scroll_page(self, direction):
        if self.feed_mode:
            # 社区页是滚动文档，不切页；而且绝不能让书页的 local_* 被"翻页"改掉
            return
        if self.is_local_mode:
            # --- 本地模式 ---
            # 【新增保护】
            if not self.local_full_text:
                return

            if direction > 0:  # 下一页
                if self.local_start_index >= len(self.local_full_text):
                    return

                # 【防跳页】状态消息（如"已恢复进度"）可能覆盖过正文，让控件内容和当前页
                # 对不上。不核对的话，几何测量会把消息长度当成页宽，落点从此固定偏移一截
                expected = self.local_full_text[self.local_start_index: self.local_start_index + 5000]
                if self.text_edit.toPlainText() != expected:
                    self.render_local_page()

                # 几何计算本页内容量
                step = self.calc_next_page_start()
                if step == 0 and self.local_start_index < len(self.local_full_text):
                    step = 1

                self.local_page_history.append(self.local_start_index)
                self.local_start_index += step

                if self.local_start_index > len(self.local_full_text):
                    self.local_start_index = len(self.local_full_text)

                self.render_local_page()

            else:  # 上一页
                if self.local_page_history:
                    # 优先使用历史
                    self.local_start_index = self.local_page_history.pop()
                else:
                    # 无历史时，反向排版计算
                    self.local_start_index = self.calc_prev_page_start()

                self.render_local_page()

            # 【关键】即时存档（安全文档/背诵模式下绝不存档：那不是书，不能污染阅读进度）
            if not self._book_veiled():
                self.config["last_local_pos"] = self.local_start_index
                self.save_config()

        else:
            # --- 网络模式 ---
            # 【关键保护】如果还没选书，直接拦截滚动，防止崩溃
            if not self.current_book:
                return

            scrollbar = self.text_edit.verticalScrollBar()
            current_val = scrollbar.value()
            max_val = scrollbar.maximum()
            min_val = scrollbar.minimum()

            target_val = current_val + (direction * (self.text_edit.viewport().height() - 30))

            if direction > 0:
                if current_val >= max_val:
                    self.next_chapter()
                else:
                    scrollbar.setValue(min(target_val, max_val))
            else:
                if current_val <= min_val:
                    self.prev_chapter()
                else:
                    scrollbar.setValue(max(target_val, min_val))

    def scroll_lines(self, direction):
        """方向键 = 逐行滚动，和滚轮完全同一套（含缓冲区边沿的衔接）。

        以前上下箭头直通 `scroll_page`：按一下整页翻，翻过头只能再翻回来；
        "往上滚一点"和"往上翻一页"还是同一个动作。行数可以在配置里调
        （`arrow_scroll_lines`，默认 3 行）——**滚轮也吃这个值**。
        """
        lines = clamp_config_int(self.config, "arrow_scroll_lines",
                                 ARROW_SCROLL_LINES, 1, ARROW_SCROLL_MAX)
        self.scroll_px(int(direction) * lines * self._line_height_px(), flip=False)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    file_config = json.load(f)
                    if "page_margin_chars" not in file_config and "page_margin" in file_config:
                        # 老配置：page_margin 是像素，等拿到当前字体再换算成"字"
                        file_config["_page_margin_legacy_px"] = file_config["page_margin"]
                    self.config = {**DEFAULT_CONFIG, **file_config}
            except:
                self.config = DEFAULT_CONFIG.copy()
        else:
            self.config = DEFAULT_CONFIG.copy()

    def save_config(self):
        try:
            self.config["window_width"] = self.width()
            self.config["window_height"] = self.height()
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f"Failed to save config: {e}")

    def on_update_text_safe(self, text, is_bottom):
        self.displaying_local_text = False
        self.set_display_text(text)
        if "加载" in text or "连接" in text or "失败" in text:
            return

        scrollbar = self.text_edit.verticalScrollBar()
        if is_bottom:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(0)

    def on_bookshelf_updated(self, books):
        self.books = books
        if self.book_selector_dialog and self.book_selector_dialog.isVisible():
            self.book_selector_dialog.update_data(books)

    def refresh_hotkeys(self):
        hotkey_str = self.config.get("boss_key", "Esc")
        peek_key_str = self.config.get("peek_key", "ctrl+e")
        safe_key_str = (self.config.get("safe_key", "ctrl+alt+d") or "").strip()
        review_key_str = (self.config.get("anki_key", "ctrl+alt+a") or "").strip()
        feed_key_str = (self.config.get("feed_key", "ctrl+alt+f") or "").strip()
        taken = set()
        try:
            keyboard.unhook_all()   # 只拆 keyboard 库自己的钩子；偷瞄/背诵的 ctypes 钩子是独立管理的
        except Exception as e:
            log_event(f"热键解绑失败: {e}")

        def _reg_global(key_str, callback, label):
            """注册一个全局热键，返回是否成功。失败（极少见）时由窗口内 QShortcut 兜底。"""
            low = (key_str or "").strip().lower()
            if not low:
                return False
            if low in taken:
                log_event(f"{label}热键与已有热键重复，已跳过: {key_str}")
                return False
            try:
                keyboard.add_hotkey(key_str, callback)
                taken.add(low)
                return True
            except Exception as e:
                log_event(f"{label}热键注册失败: {e}")
                return False

        # **四个模式键都注册成全局热键**：老板键、偷瞄、安全文档、背诵。窗口不在前台时也得
        # 能按——尤其老板键，窗口藏起来之后它更不可能是前台窗口，只能靠全局热键叫回来。
        # 库的回调跑在 keyboard 库自己的分发线程上（那里不能碰界面），所以一律发信号回主线程。
        g_boss = _reg_global(hotkey_str, self.on_global_hotkey_triggered, "老板键")
        g_peek = _reg_global(peek_key_str, self.on_peek_hotkey_triggered, "偷瞄")
        g_safe = _reg_global(safe_key_str, self.on_safe_hotkey_triggered, "安全文档")
        g_review = _reg_global(review_key_str, self.on_review_hotkey_triggered, "背诵")
        g_feed = _reg_global(feed_key_str, self.on_feed_hotkey_triggered, "社区")
        # 松键回调也重挂一遍（unhook_all 会把它一起拆掉）：热键"同一下只认第一次"靠它复位。
        try:
            keyboard.on_release(self.on_hotkey_release)
        except Exception as e:
            log_event(f"松键监听注册失败: {e}")
        # 说明：全局热键和 QShortcut 只能留一条路。两条都留的话，窗口是前台时同一个键会被
        # 触发两次（老板键收起又立刻弹出），所以下面只在"全局注册失败"时才建 QShortcut。
        try:
            if self.local_shortcut:
                self.local_shortcut.setKey(QKeySequence())
                self.local_shortcut = None
            if not g_boss:
                self.local_shortcut = QShortcut(QKeySequence(hotkey_str), self)
                self.local_shortcut.setAutoRepeat(False)   # 按住不放时 Qt 也会重复触发同一个快捷键
                self.local_shortcut.activated.connect(lambda: self.toggle_window("shortcut"))
        except:
            pass
        try:
            if self.peek_shortcut:
                self.peek_shortcut.setKey(QKeySequence())
                self.peek_shortcut = None
            if not g_peek:
                self.peek_shortcut = QShortcut(QKeySequence(peek_key_str), self)
                self.peek_shortcut.setAutoRepeat(False)
                self.peek_shortcut.activated.connect(self.toggle_peek)
        except:
            pass
        try:
            if self.safe_shortcut:
                self.safe_shortcut.setKey(QKeySequence())
                self.safe_shortcut = None
            if not g_safe and safe_key_str:
                self.safe_shortcut = QShortcut(QKeySequence(safe_key_str), self)
                self.safe_shortcut.setAutoRepeat(False)
                self.safe_shortcut.activated.connect(self.toggle_safe_mode)
        except:
            pass
        try:
            if self.review_shortcut:
                self.review_shortcut.setKey(QKeySequence())
                self.review_shortcut = None
            if not g_review and review_key_str:
                self.review_shortcut = QShortcut(QKeySequence(review_key_str), self)
                self.review_shortcut.setAutoRepeat(False)
                self.review_shortcut.activated.connect(self.toggle_review_mode)
        except:
            pass
        try:
            if self.feed_shortcut:
                self.feed_shortcut.setKey(QKeySequence())
                self.feed_shortcut = None
            if not g_feed and feed_key_str:
                self.feed_shortcut = QShortcut(QKeySequence(feed_key_str), self)
                self.feed_shortcut.setAutoRepeat(False)
                self.feed_shortcut.activated.connect(self.toggle_feed_mode)
        except:
            pass

        # 划线与书签的窗口内快捷键，只注册一次（老板键看门狗会反复调 refresh_hotkeys）
        if getattr(self, "marks_shortcuts", None) is None:
            try:
                self.marks_shortcuts = [
                    QShortcut(QKeySequence("Ctrl+B"), self, activated=self.toggle_bookmark),
                    QShortcut(QKeySequence("Ctrl+L"), self,
                              activated=lambda: self.open_reading_marks(1)),
                ]
            except Exception:
                self.marks_shortcuts = []

    # 热键"重复流"中断多久算这一下按完了（秒）。按住不放时系统每 30~60 毫秒来一次重复，
    # 而 Windows 的**首次重复延迟**大约 0.5 秒——所以这个值必须比 0.5 大，1.0 秒足够稳。
    HOTKEY_REPEAT_MAX = 1.0

    def hotkey_is_repeat(self, tag, key_str):
        """同一下按键只认第一次。**True = 这一下是按住不放的系统重复，丢掉。**

        按住键不放时 Windows 会一直重复发 keydown（自动重复），`keyboard` 库的热键也就跟着
        连发：老板键会被连点成"显示-隐藏-显示-隐藏"，在用户眼里就是**唤出时闪一下又不见了**
        （2026-10-16 实测：按住 ctrl+s 一下 = 12 次触发）。原来只有 0.3 秒的时间防连击，
        挡不住隔了 0.3 秒以后的重复。

        判据是"**这一下按的那个键松开了没有**"：`on_hotkey_release`（keyboard 库的松键回调）
        负责清标记。所以按一下、按多久、连按多快都不误伤，只把"没松手"的重复挡掉。
        跑在库的分发线程上，只碰纯 Python 状态，不碰界面、不开定时器。
        """
        trig = (key_str or "").lower().replace(" ", "").split("+")[-1]
        if not trig:
            return False
        gate = getattr(self, "_hotkey_gate", None)
        if gate is None:
            return False
        if gate.get(tag) == trig:
            # 只要"重复流"还在（每次重复的间隔远小于这个阈值），就把闸门续期——
            # 按住多久都只算一下。真的停下来了（松手，或松键回调丢了之后键早就不动了），
            # 阈值一过，下一次按下就是新的一下：绝不会把热键永久锁死。
            if time.time() - self._hotkey_gate_at.get(tag, 0.0) < self.HOTKEY_REPEAT_MAX:
                self._hotkey_gate_at[tag] = time.time()
                return True
        gate[tag] = trig
        self._hotkey_gate_at[tag] = time.time()
        return False

    def on_hotkey_release(self, event):
        """keyboard 库的松键回调：把"这一下按过了"的标记清掉，下一次按下才算新的一下。

        同样跑在库的分发线程上：只用纯 Python 状态，绝不碰界面、绝不开定时器。
        """
        name = (getattr(event, "name", "") or "").lower()
        if not name:
            return
        gate = getattr(self, "_hotkey_gate", None)
        if not gate:
            return
        for tag in [t for t, k in list(gate.items()) if k == name]:
            gate.pop(tag, None)
            self._hotkey_gate_at.pop(tag, None)

    def on_global_hotkey_triggered(self):
        # 这四个回调都跑在 keyboard 库的分发线程上，那里没有兜底 try：回调抛异常（比如日志
        # 写盘失败）会把分发线程直接打死，此后线程活着、listening 仍是 True，热键却一个都不响。
        # 所以自己兜住，绝不把异常抛回库里。
        try:
            if self.hotkey_is_repeat("boss", self.config.get("boss_key", "")):
                return
            log_event(f"老板键触发 boss_key={self.config.get('boss_key')}")
            self.hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"老板键回调异常: {e}")
            except Exception:
                pass

    def on_peek_hotkey_triggered(self):
        try:
            if self.hotkey_is_repeat("peek", self.config.get("peek_key", "")):
                return
            self.peek_hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"偷瞄键回调异常: {e}")
            except Exception:
                pass

    def on_safe_hotkey_triggered(self):
        try:
            if self.hotkey_is_repeat("safe", self.config.get("safe_key", "")):
                return
            log_event("安全文档热键触发")
            self.safe_hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"安全文档键回调异常: {e}")
            except Exception:
                pass

    def on_review_hotkey_triggered(self):
        try:
            if self.hotkey_is_repeat("review", self.config.get("anki_key", "")):
                return
            log_event("隐形背诵热键触发")
            self.review_hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"背诵键回调异常: {e}")
            except Exception:
                pass

    def on_feed_hotkey_triggered(self):
        try:
            if self.hotkey_is_repeat("feed", self.config.get("feed_key", "")):
                return
            log_event("社区统一流热键触发")
            self.feed_hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"社区键回调异常: {e}")
            except Exception:
                pass

    def register_session_notification(self):
        """让窗口收到锁屏/解锁通知（WM_WTSSESSION_CHANGE）——解锁后热键自愈的触发点。"""
        try:
            if not ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
                    int(self.winId()), NOTIFY_FOR_THIS_SESSION):
                raise ctypes.WinError(ctypes.get_last_error())
            log_event("已注册会话通知（锁屏解锁后自动重挂全局热键）")
        except Exception as e:
            log_event(f"会话通知注册失败（只能靠看门狗定期重挂）: {e}")

    def clear_stuck_keys(self):
        """擦掉"其实早松开了、库却以为还按着"的键——锁屏后热键全失灵的真凶。

        锁屏（Win+L）那一刻输入进了 Winlogon 的安全桌面，低级钩子收不到 KEY_UP，于是 Win
        永远留在 keyboard 库的 `_pressed_events` 里。而热键匹配用的是"当前按下键的排序元组"
        （库内部 `tuple(sorted(_pressed_events))` 去查 `nonblocking_hotkeys`），多一个 Win
        就永远匹配不上 ctrl+d / ctrl+e / ctrl+alt+d——于是老板键、偷瞄键、安全文档键一起失灵，
        只剩托盘能用；而线程还活着、listening 仍是 True，看门狗光看状态查不出任何异常。
        解锁和每次例行重挂时都清一遍（顺手把库里另一份"逻辑按下"表也清了）。"""
        cleared = []
        lock = getattr(keyboard, "_pressed_events_lock", None)
        for name in ("_pressed_events", "_logically_pressed_keys"):
            state = getattr(keyboard, name, None)
            if not isinstance(state, dict) or not state:
                continue
            cleared.append(f"{name}={sorted(state)}")
            try:
                if lock is not None:
                    with lock:
                        state.clear()
                else:
                    state.clear()
            except Exception:
                pass
        if cleared:
            log_event("清掉卡住的按键状态（锁屏时松开的键没收到事件）: " + ", ".join(cleared))

    def recover_hotkeys(self, reason=""):
        """把全局热键整套重挂一遍。

        锁屏解锁、监听线程死掉、钩子被系统摘掉，最后都走这里。两件事：先擦掉卡住的按键
        状态（见 clear_stuck_keys），再重挂热键。偷瞄模式的 ctypes 钩子是另一套
        （unhook_all 拆不到它），在偷瞄状态下一并重挂。reason 为空表示这是看门狗的例行
        静默重挂，不往日志里写——免得日志被心跳刷满。"""
        try:
            self.clear_stuck_keys()
            self.refresh_hotkeys()
            if self.peek_mode:
                self.peek_input_hook.install(self.config.get("peek_key", "ctrl+e"))
            if reason:
                log_event(f"全局热键已重挂（{reason}）")
        except Exception as e:
            log_event(f"重挂全局热键失败: {e}")

    def check_hotkeys_alive(self):
        """老板键看门狗。两种病都得治：

        1. 监听线程死了 / 根本没在监听 → 立刻重挂（老逻辑，锁屏、睡眠、钩子被强拆后常见）。
        2. **看起来一切正常也要定期重挂**——这才是"锁屏解锁后 cd/ce 全失灵、托盘却还能用"的真凶：
           Windows 对低级钩子有 LowLevelHooksTimeout（默认 300ms），回调被卡超时就把
           WH_KEYBOARD_LL **悄悄摘掉**，而 keyboard 库毫不知情（listening 仍是 True、线程
           还活着），光查状态永远查不出来。所以每两拍（约 90 秒）无条件重挂一次，
           代价是重挂瞬间几毫秒内的按键可能漏一次热键（按键本身照常进别的程序）。"""
        try:
            listener = getattr(keyboard, "_listener", None)
            if listener is None or not listener.listening:
                self.recover_hotkeys("监听未在运行")
                return
            thread = getattr(listener, "listening_thread", None)
            if thread is not None and not thread.is_alive():
                self.recover_hotkeys("监听线程已死")
                return
            # 分发线程：真正把事件派给热键回调的是 processing_thread，它里面没有兜底 try，
            # 回调抛异常就会把它打死——此后线程活着、listening 还是 True，热键却一个都不响。
            proc = getattr(listener, "processing_thread", None)
            if proc is not None and not proc.is_alive():
                log_event("热键分发线程已死，重新拉起")
                self.restart_dispatch_thread(listener)
                self.recover_hotkeys("分发线程已死")
                return
            self.hotkey_reregister_tick += 1
            if self.hotkey_reregister_tick >= 2:
                self.hotkey_reregister_tick = 0
                self.recover_hotkeys()
        except Exception as e:
            log_event(f"热键看门狗异常: {e}")

    def restart_dispatch_thread(self, listener):
        """processing_thread 死了就补一条（直接改 listening 会让钩子线程也重启，反而装出两个钩子）。"""
        try:
            queue = getattr(listener, "queue", None)
            if queue is not None:      # 先把积压的事件倒掉，免得补上来的线程把旧按键重新触发一遍
                for _ in range(10000):
                    try:
                        queue.get_nowait()
                    except Exception:
                        break
            listener.processing_thread = threading.Thread(target=listener.process, daemon=True)
            listener.processing_thread.start()
            log_event("已重新拉起热键分发线程")
        except Exception as e:
            log_event(f"重新拉起热键分发线程失败: {e}")

    def nativeEvent(self, eventType, message):
        """接 Windows 会话通知：解锁/登录后主动重挂全局热键。

        锁屏和解锁那一瞬间，钩子回调会被系统卡住，一旦超过 LowLevelHooksTimeout，
        WH_KEYBOARD_LL 就被摘掉且不给任何通知（见 check_hotkeys_alive）。解锁是个明确信号，
        收到后延迟 1.2 秒（等桌面切回来、钩子线程缓过来）无条件重挂一次。"""
        try:
            if eventType == b"windows_generic_MSG":
                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
                if msg.message == WM_WTSSESSION_CHANGE and msg.wParam in (WTS_SESSION_UNLOCK, WTS_SESSION_LOGON):
                    log_event("检测到解锁/登录，稍后重挂全局热键")
                    QTimer.singleShot(800, lambda: self.recover_hotkeys("解锁/登录后"))
                    # 解锁时 Windows 可能已经把窗口挪到桌面外（锁屏期间显示器断电/分辨率抖动），
                    # 窗口当时若还开着，顺手拉回它该在的位置；藏着的话等下次 show 自动处理
                    QTimer.singleShot(900, self.ensure_on_screen)
        except Exception:
            pass
        return super().nativeEvent(eventType, message)

    def initUI(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # 偷瞄模式浮现时不抢焦点；老板键路径仍显式 activateWindow
        self.setMouseTracking(True)

        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.content_frame = CornerFrame()
        self.content_layout = QVBoxLayout(self.content_frame)

        # 不再给左右各加 5px 外框留白：那会让左右比上下各多 5px，四边就对不齐了。
        # （圆角是 5px，但页边距本身至少也有几像素，正文不会贴到圆角上）
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)

        self.text_edit = ReaderTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFrameStyle(QFrame.NoFrame)
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 要能拖选才能划线；选中不改正文，只用来算Range。焦点策略保持 NoFocus，
        # 免得窗口抢走工作窗口的键盘焦点。
        self.text_edit.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_edit.setFocusPolicy(Qt.NoFocus)
        # 正文区不是输入框：光标一律箭头（缩放区/拖动时临时换形状）
        self.text_edit.set_pinned_cursor(Qt.ArrowCursor)
        # 右键菜单交给我们自己：划到高亮上给划线菜单，正文上给窗口菜单
        self.text_edit.setContextMenuPolicy(Qt.NoContextMenu)

        self.text_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.text_edit.setMinimumHeight(0)
        self.text_edit.document().setDocumentMargin(0)

        self.text_edit.installEventFilter(self)
        # 视口滚一下就记一次阅读进度（页内滚轮/偷瞄滚轮只动视口，local_start_index 不动）
        self.text_edit.verticalScrollBar().valueChanged.connect(self.record_view_position)
        # 列表页滚到底就续下一页（"页面下滑到底了继续加载"）：不用按 ↓ 把光标挪下去才会加载
        self.text_edit.verticalScrollBar().valueChanged.connect(self.feed_autoload_more)
        # 鼠标事件其实都发给 viewport（上次滚轮那个坑），所以划选/单击必须装在 viewport 上
        self.text_edit.viewport().installEventFilter(self)
        self.text_edit.viewport().setMouseTracking(True)
        # 悬停事件是兜住光标用的：真实鼠标移动时它的处理顺序在最后
        self.text_edit.viewport().setAttribute(Qt.WA_Hover, True)

        self.content_layout.addWidget(self.text_edit)

        # 背诵专用页脚：**只有文字，没有按钮**。真人原话："不用按钮，有有效信息就行"——
        # 评分靠 1-4 键和右键菜单，页脚只要把「还剩多少」和「按下去会变成什么」显示出来：
        #     新 20 · 学 4 · 复 160　今日已 1
        #     1 重来 <1 分　2 困难 <6 分　3 良好 <10 分　4 容易 9 天
        # 折行由 fill_review_bar 自己按字号算（不用 QLabel 的 wordWrap，否则会在
        # "1 重来"和"<1 分"之间断开），所以高度严格等于行数，不留空行、不占固定高度。
        # 平时整条藏着；安全文档盖上来时也藏着——周报底下挂一行评分信息，一眼就露馅。
        self.review_bar = QWidget()
        self.review_bar.setObjectName("reviewBar")
        # 竖向 Maximum：页脚永远只占它需要的那一点高度。默认策略下 QVBoxLayout 会把
        # 多余空间也分给它（实测占了半个窗口高），正文区就被压掉一半。
        self.review_bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        bar_layout = QVBoxLayout(self.review_bar)
        bar_layout.setContentsMargins(6, 2, 6, 3)
        bar_layout.setSpacing(0)
        self.review_info_label = QLabel("")
        # 横向 Ignored：窗口窄的时候文字自己收缩，绝不把窗口顶宽（Fixed 策略曾把
        # top-level 的 minimumSizeHint 从 69 顶到 494，进背诵瞬间窗口 429→494）
        self.review_info_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)
        self.review_info_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.review_info_label.setTextInteractionFlags(Qt.NoTextInteraction)
        bar_layout.addWidget(self.review_info_label)
        self.review_bar.setVisible(False)
        self.content_layout.addWidget(self.review_bar)

        self.main_layout.addWidget(self.content_frame)
        self.setLayout(self.main_layout)

        w = self.config.get("window_width", 400)
        h = self.config.get("window_height", 300)
        self.resize(w, h)
        # 恢复上次关闭时的位置；没存过或位置已不在当前桌面（比如拔了显示器）才回默认
        sx, sy = self.config.get("win_x"), self.config.get("win_y")
        if isinstance(sx, int) and isinstance(sy, int) and self._point_on_desktop(sx, sy):
            self.move(sx, sy)
        else:
            self.move(100, 100)
        self.oldPos = self.pos()

        self.apply_style()

    def update_viewport_cursor(self, event):
        """正文区光标：默认箭头（不是输入框的 I 形）；拖动中/右下角缩放区给对应光标。"""
        if self.filter_drag_origin is not None:
            want = Qt.SizeAllCursor
        elif (event.buttons() == Qt.NoButton
              and self.is_in_resize_area(self.text_edit.viewport().mapTo(self, event.pos()))):
            want = Qt.SizeFDiagCursor
        elif event.buttons() == Qt.NoButton and self.feed_hover_title(event.pos()):
            want = Qt.PointingHandCursor     # 标题能点（知乎回答页 → 该问题的全部回答）
        else:
            want = Qt.ArrowCursor
        self.text_edit.set_pinned_cursor(want)

    def eventFilter(self, source, event):
        # 划选 / 单击划线 / 中键拖窗口都在 viewport 上收（滚轮那个坑：事件不发往 text_edit 本身）
        if source is self.text_edit.viewport():
            if event.type() == QEvent.MouseMove:
                self.update_viewport_cursor(event)
            elif event.type() in (QEvent.Leave, QEvent.HoverLeave):
                # 指针离开正文区：形状恢复默认，免得缩放光标被带出去
                self.text_edit.set_pinned_cursor(Qt.ArrowCursor)
            # 偷瞄模式下这些事件照旧往下走：左键已经不会退出偷瞄了，
            # 所以正文区要能正常划选、点击（选区/划线面板都按常规流程处理）。
            if event.type() == QEvent.MouseButtonDblClick and self.peek_mode:
                # 偷瞄时双击正文 = 换成安全文档（窗口继续亮着、偷瞄状态保持，只换内容）
                self.enter_safe_mode()
                return True
            if event.type() == QEvent.MouseButtonDblClick and self.feed_mode:
                # 社区页：双击 = 进去/折叠（列表现在单击就进，双击走 force 直接做动作，
                # 不会被"已经选中"当成第二次单击）
                self.feed_click(event.pos(), force=True)
                self.clear_text_selection()
                return True
            if event.type() == QEvent.MouseButtonPress:
                # 按下这一刻记一下有没有选区：鼠标一按选区就没了，等到 release 再问就问不出来
                self._feed_press_had_sel = bool(self.text_edit.textCursor().hasSelection())
            if event.type() == QEvent.MouseButtonPress and self.on_viewport_press(event):
                return True
            if event.type() == QEvent.MouseMove and self.on_viewport_move(event):
                return True
            if event.type() == QEvent.MouseButtonRelease and self.on_viewport_release(event):
                return True
        # 滚轮：**viewport 和 text_edit 两条路都要接**。Qt 把滚轮发给谁是不一定的
        # （鼠标停在正文上一般落在 viewport，焦点在控件上时可能落在 text_edit），以前只接了
        # text_edit，于是本地书页"有时候滚一行、有时候翻一整页"（真人 2026-10-19 反馈）。
        if (event.type() == QEvent.Wheel
                and (source is self.text_edit or source is self.text_edit.viewport())):
            delta = event.angleDelta().y()
            if self.feed_mode:
                # 横着看：触摸板左右滑（dx）和 Shift+滚轮都用来左右滑，
                # 一次走"滚动行数"那么多行的高度；这一页没有横向溢出就照旧上下滚。
                dx = int(event.angleDelta().x())
                if dx:
                    if self._feed_hslide(dx):
                        return True
                elif delta and (event.modifiers() & Qt.ShiftModifier):
                    step = self._feed_hstep()
                    if self._feed_hslide(-step if delta > 0 else step):
                        return True
            if not delta:
                return super().eventFilter(source, event)     # 横向滚轮/倾斜轮：不抢
            if self.review_mode or self.feed_mode:
                # 卡片页、社区页：滚它们自己（同样是设置里那几行），不接页、不碰书页进度。
                # **这条要排在"没选书就拦住"前面**：社区页跟书没关系，没选书也得能滚
                # （以前排在后面，没选书时托盘里进社区页滚轮是死的）。
                self._local_wheel_step(delta)
                return True
            # 【关键保护】如果既没选书，也不是本地模式，直接拦截滚轮不处理
            if not self.is_local_mode and not self.current_book:
                return True

            # 本地书页（到缓冲区边沿自动衔接）和网络书（滚不动才换章）都走这一条，
            # 一格滚多少行由设置决定
            self._local_wheel_step(delta)
            return True
        return super().eventFilter(source, event)

    def _point_on_desktop(self, x, y):
        """坐标是否还落在某块屏幕上（虚拟桌面并集内），防止换了显示器窗口飞出屏。"""
        union = QRect()
        for scr in QApplication.instance().screens():
            union = union.united(scr.geometry())
        return not union.isNull() and union.contains(x, y)

    def save_window_pos(self):
        """记住窗口当前位置，下次启动原位出现。

        **只记"确实落在某块真实屏幕上"的位置**：锁屏解锁、拔显示器、缩放变化之后，Windows 会把
        窗口丢到桌面之外；这时候照着记下来，就是把一个坏坐标写进配置，下次召唤又按它摆一次、
        再被 ensure_on_screen 挪回来——日志里"窗口被挪到屏幕外，已挪回…"于是每次显示都要重来一遍
        （2026-10-16 实测过这个循环）。宁可不更新，也不存坏坐标。
        """
        try:
            if not window_on_real_screen(int(self.winId())):
                return
            self.config["win_x"] = self.x()
            self.config["win_y"] = self.y()
            self.save_config()
        except Exception:
            pass

    def sync_qt_pos_from_win32(self, hwnd):
        """Win32 挪完窗口后，把 Qt 缓存的坐标改成同一个点。

        Qt 用的是逻辑像素，系统缩放 150%/200% 时和物理像素差一个倍数。不同步的话
        Qt 还揣着旧位置，下一次 show / 样式刷新就把窗口拉回屏幕外——日志里
        "已强制挪回主屏"会在每次显示时重复出现，就是没同步留下的病根。"""
        try:
            rect = window_rect(hwnd)
            if not rect:
                return
            dpr = float(self.devicePixelRatioF()) or 1.0
            self.move(int(round(rect[0] / dpr)), int(round(rect[1] / dpr)))
        except Exception:
            pass

    def _rect_usable(self, rect):
        """这块矩形，人**看得见、够得着**吗？

        判据取"和某块屏幕的工作区有足够的重叠"：宽高各至少露出一半、或至少露出
        `MIN_VISIBLE` 那么多。以前这里问的是 Win32 的 `MonitorFromWindow`——
        窗口刚 show 的那一瞬它经常还答"不在任何屏幕上"，于是每唤出一次就白挪一次，
        用户看到的是"窗口跳了一下/闪一下就跑到别处"。
        """
        if rect is None or rect.width() <= 0 or rect.height() <= 0:
            return False
        need_w = max(1, min(rect.width() // 2, self.MIN_VISIBLE))
        need_h = max(1, min(rect.height() // 2, self.MIN_VISIBLE))
        try:
            screens = QApplication.instance().screens() or []
        except Exception:
            screens = []
        for scr in screens:
            avail = scr.availableGeometry()
            inter = avail.intersected(rect)
            if inter.width() >= need_w and inter.height() >= need_h:
                return True
        return False

    MIN_VISIBLE = 120        # 至少露出这么多像素才算"在屏幕上"

    def ensure_on_screen(self):
        """召唤/浮现后确保窗口在**用户看得见的地方**，看不见才动它。

        锁屏解锁、显示器断电、拔过显示器之后，Windows 会把窗口丢到桌面之外（实测见过
        x = 上次位置 − 一整屏宽）。这里用 Qt 自己的几何判断"看不看得见"，顺序是：
          1. 已经看得见 → 一个像素都不动（这是绝大多数情况，**以前却是每次都挪**）；
          2. 配置里记住的位置（用户自己摆的地方）；
          3. 主屏工作区左上角 +60；
          4. 都不行才退回 Win32 物理兜底，并把 Qt 坐标同步过来。
        前两步都用 Qt 的 `move()`：逻辑像素，多屏 DPI 由 Qt 自己换算。
        """
        try:
            if not self.isVisible():
                return              # 窗口藏着的时候别动它，免得和下一次 show 打架
            geo = self.frameGeometry()
            if geo.width() <= 0 or geo.height() <= 0:
                # 刚 show 的那一瞬几何还没算完。这时候判断一定是"看不见"，挪一次就白挪一次
                # ——日志里"窗口被挪到屏幕外"每次显示都刷一遍，就是这么来的。
                # 等这一轮事件走完再判，最多等两次，免得卡进循环。
                if getattr(self, "_ensure_retries", 0) < 2:
                    self._ensure_retries = getattr(self, "_ensure_retries", 0) + 1
                    QTimer.singleShot(0, self.ensure_on_screen)
                return
            self._ensure_retries = 0
            if self._rect_usable(geo):
                return
            old = (self.x(), self.y())
            candidates = []
            sx, sy = self.config.get("win_x"), self.config.get("win_y")
            if isinstance(sx, int) and isinstance(sy, int) and self._point_on_desktop(sx, sy):
                candidates.append((sx, sy, "上次的位置"))
            try:
                avail = QApplication.primaryScreen().availableGeometry()
                candidates.append((avail.left() + 60, avail.top() + 60, "主屏工作区"))
            except Exception:
                pass
            for x, y, label in candidates:
                # 用算出来的矩形判断，不回头读 frameGeometry：move() 之后它要下一帧才更新
                if self._rect_usable(QRect(x, y, geo.width(), geo.height())):
                    self.move(x, y)
                    log_event("窗口不在可见范围内，已挪回%s (%d,%d)，原来在 (%d,%d)"
                              % (label, x, y, old[0], old[1]))
                    return
            hwnd = int(self.winId())
            if force_window_to_primary(hwnd):
                self.sync_qt_pos_from_win32(hwnd)
                log_event("窗口不在任何真实屏幕上，已用 Win32 挪回主屏并同步 Qt 坐标")
            else:
                log_event("窗口不在任何真实屏幕上，但挪回主屏失败")
        except Exception as e:
            log_event(f"屏幕校验异常: {e}")

    def initTray(self):
        self.tray_icon = QSystemTrayIcon(self)
        # 极简图标：一枚白色书签（圆角条 + 底部三角缺口），透明底，别再放字和色块
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(245, 245, 245))
        path = QPainterPath()
        path.addRoundedRect(21, 6, 22, 52, 5, 5)
        notch = QPainterPath()
        notch.moveTo(27, 58)
        notch.lineTo(32, 44)
        notch.lineTo(37, 58)
        notch.closeSubpath()
        painter.drawPath(path.subtracted(notch))
        painter.end()
        self.tray_icon.setIcon(QIcon(pixmap))
        self.tray_icon.setToolTip("StealthReader 阅读器（进程运行中）")
        tray_menu = QMenu()
        tray_menu.addAction("显示/隐藏（老板键）").triggered.connect(
            lambda _checked=False: self.toggle_window("tray"))
        tray_menu.addAction("隐形背诵（Anki 卡片）").triggered.connect(self.toggle_review_mode)
        tray_menu.addAction("社区统一流（知乎/V2EX…）").triggered.connect(self.toggle_feed_mode)
        tray_menu.addAction("叫回 Anki 窗口").triggered.connect(self.rescue_anki_window)
        tray_menu.addAction("打开本地书籍…").triggered.connect(self.open_local_file_dialog)
        tray_menu.addAction("设置…").triggered.connect(self.open_settings)
        tray_menu.addSeparator()
        tray_menu.addAction("退出").triggered.connect(self.quit_app)
        modernize_menu(tray_menu)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()
        # 图标出现在任务栏角溢出区（^）里，右键任务栏 → 任务栏设置 → 任务栏角溢出，
        # 把 StealthReader 打开就能常驻右下角

    def on_tray_activated(self, reason):
        # 单击就切换显隐，双击太绕
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle_window("tray")

    # 刚唤出的这一小段里，任何"收起"都当成重复触发忽略掉。
    # 真人再快也不会 0.45 秒内按两下；而"唤出后没抢到前台"时，系统/别的软件可能立刻替它
    # 把窗口收走，用户看到的就是"闪一下就没了"。忽略的代价只是"再按一次"，
    # 反过来（窗口自己吞掉唤出）就没法用了。
    WAKE_GRACE = 0.45

    def showEvent(self, event):
        """窗口每次露脸都记一笔时间——`toggle_window` 靠它认出"刚唤出的那一下"。

        写在 `showEvent` 里，而不是只写在老板键那条分支上：启动、托盘、偷瞄，以及以后
        新加的任何显示路径都要经过这里，一处就覆盖全了。（以前只有老板键那条记，
        于是"启动后立刻按老板键"那一下完全不受临界期保护。）
        """
        self._shown_at = time.time()
        super().showEvent(event)

    def toggle_window(self, source="boss"):
        current_time = time.time()
        if current_time - self.last_toggle_time < 0.3:
            return
        if self.isVisible():
            shown_for = current_time - getattr(self, "_shown_at", 0.0)
            if shown_for < self.WAKE_GRACE:
                # 2026-10-19 实测日志：解锁后一口气出现"触发→隐藏、触发→显示、触发→隐藏"，
                # 每次显示都紧跟一次收起，界面上的观感就是"按唤醒、闪一下、没了"。
                # 这一下不认，让它留在屏幕上。
                log_event("忽略一次收起（%.0fms 前刚唤出，来源 %s）"
                          % (shown_for * 1000, source))
                return
        self.last_toggle_time = current_time

        if self.isVisible():
            log_event("老板键 -> 隐藏窗口（来源 %s）" % source)
            if self.peek_mode:
                # 偷瞄状态里被收起来：钩子先卸掉，否则下次唤出后"随便点一下"就被判成
                # 退出偷瞄，窗口刚露脸又被收走（日志里 14:14:12 就是这么一闪的）。
                self._drop_peek_state()
            self.dismiss_action_panel(cancelled=True)   # 连同待确认的划选一起作废
            self.sync_progress_async()
            if self.is_local_mode and not self._book_veiled():
                # 收起来 = "先看到这儿"：记视口顶那一行，而不是本页页首（页内滚过会差一屏）
                self.config["last_local_pos"] = self.current_top_offset()
            self.save_window_pos()
            self.hide()
        else:
            log_event("老板键 -> 显示窗口（来源 %s）" % source)
            if self.peek_mode:
                # 老板键接管可见层：把偷瞄那套收干净再说，免得它把接下来的点击当退出指令
                self._drop_peek_state()
            self.showNormal()
            self.apply_style()
            self.activateWindow()
            self.ensure_on_screen()
            if self.config.get("auto_mode", False):
                self.chameleon_timer.start()
                self.adjust_color_to_background()

            if self.config.get("antishot_mode", False):
                set_window_protection(int(self.winId()), True)

    # --- 偷瞄模式：Ctrl+E 浮现，滚轮在任何地方都能翻页，其他输入一律退出 ---
    def toggle_peek(self):
        """偷瞄键：**只动可见层，绝不换页**。

        当前是哪一页（书页 / 卡片页 / 安全文档页）就浮现哪一页——和老板键一样，
        可见层与内容层正交。以前这里会把背诵/安全文档先退掉（"偷瞄的永远是书"），
        在卡片页按一下偷瞄键就得重新进牌组，会话、计数、撤销上下文全丢，是错的。
        """
        now = time.time()
        if now - self.peek_last_toggle < 0.3:
            return
        self.peek_last_toggle = now
        if self.peek_mode:
            self.exit_peek("peek_key")
            return
        if now - self.peek_last_exit < 0.35:
            return  # 刚退出的那一下按键余波，别当成新指令再进去
        self.enter_peek()

    def enter_peek(self):
        """浮现但不抢焦点，挂全局滚轮。挂不上钩子就不进——半吊子的偷瞄只会让人困惑。

        不碰内容层：屏幕上是哪一页就浮现哪一页（卡片页 = 那张卡、安全文档页 = 那份文档）。"""
        if self.peek_mode:
            return
        if not self.peek_input_hook.install(self.config.get("peek_key", "ctrl+e")):
            print("偷瞄模式未启动：全局输入钩子安装失败")
            return
        self.dismiss_action_panel(cancelled=True)   # 进偷瞄时收掉已经在的面板
        self.peek_was_visible = self.isVisible()
        self.peek_mode = True
        self.text_edit.set_pinned_cursor(Qt.ArrowCursor)   # 模式切换后光标形状回到默认
        if not self.isVisible():
            self.show()  # WA_ShowWithoutActivating：不夺走工作窗口的焦点
        self.raise_()
        self.apply_style()
        self.ensure_on_screen()
        if self.config.get("auto_mode", False):
            self.chameleon_timer.start()
            self.adjust_color_to_background()
        if self.config.get("antishot_mode", False):
            set_window_protection(int(self.winId()), True)

    def _drop_peek_state(self):
        """从偷瞄状态里退出，但**不动可见层**（收起来还是留着，由调用方决定）。

        偷瞄的输入钩子是这样一条规矩：屏幕上出现"别的输入"就当用户要走，立刻收窗口。
        所以一旦有人在偷瞄状态里用老板键/托盘接管了可见层，钩子必须一起卸掉——
        否则窗口刚露脸，紧跟着来的一下点击就被判成"退出偷瞄"，窗口当场没了。
        """
        if not self.peek_mode:
            return
        self.peek_mode = False
        self.peek_last_exit = time.time()
        try:
            self.peek_input_hook.uninstall()
        except Exception:
            pass
        try:
            self.text_edit.set_pinned_cursor(Qt.ArrowCursor)
        except Exception:
            pass

    def exit_peek(self, reason=""):
        """退出偷瞄 = **把窗口收起来**（等价于老板键的隐藏），内容层一个字都不动。

        退出去时屏幕上是哪一页，回来还是哪一页：卡片页仍是那张卡（会话/计数/撤销都在），
        安全文档页仍是那份文档。要再叫出来：Ctrl+E 继续偷瞄，或点托盘图标
        （窗口藏起来时不可能是前台窗口，所以老板键那会儿按不了）。"""
        if not self.peek_mode:
            return
        self._drop_peek_state()
        self.apply_style()                # 先恢复正常样式，免得偷瞄那套样式留到下次露脸
        self.sync_progress_async()
        self.hide()
        log_event(f"偷瞄退出（{reason or '未说明'}）→ 窗口已隐藏")

    def on_peek_wheel(self, delta):
        """偷瞄模式的滚轮 = 窗口内的手感：一格还是设置里那几行，到边沿才衔接前后页。

        卡片页/社区页也只滚自己（它们的 local_* 是空壳，一个字都不许碰）。"""
        self._local_wheel_step(delta)

    def _line_height_px(self):
        """一行字在文档里实际占多少像素（跟着字号、行距走）。"""
        try:
            h = int(round(self.text_edit.cursorRect(self.text_edit.textCursor()).height()))
        except Exception:
            h = 0
        if 6 <= h <= 400:
            return h
        return max(6, int(self.text_edit.fontMetrics().height()))

    def scroll_px(self, px, flip=True):
        """按像素滚正文——滚轮和方向键共用这一条，滚多少行由设置决定。

        `flip=False`（滚轮 / 上下方向键）：滚到顶就**停住**，绝不自动翻页、绝不换章。
        真人："有时候向上滚动还是会变成翻了一整页，我希望这个永远不出现。"
        往前/往后翻整页只留给**明确的翻页键**（空格 / PageUp / PageDown）。"""
        if not px:
            return
        sb = self.text_edit.verticalScrollBar()
        before = sb.value()
        sb.setValue(int(round(before + px)))
        moved = False

        if self.feed_mode or (self.review_mode and not self.safe_mode):
            return                          # 社区/卡片：滚它们自己，不接页、不碰书页进度
        if self.is_local_mode and self.local_full_text:
            if not flip:
                return                      # 滚轮：滚到边就停，别悄悄换一屏文字
            if px > 0 and sb.maximum() > 0 and sb.value() >= sb.maximum():
                # 缓冲区到底：下一页从"当前视口顶部"衔接，已看的字不重不漏
                cursor = self.text_edit.cursorForPosition(QPoint(0, 0))
                new_start = min(self.local_start_index + cursor.position(),
                                max(0, len(self.local_full_text) - 1))
                if new_start > self.local_start_index:
                    self.local_page_history.append(self.local_start_index)
                    self.local_start_index = new_start
                    self.render_local_page()
                    moved = True
            elif px < 0 and sb.value() <= 0:
                # 缓冲区到顶：渲染更早的 5000 字并停在其底部，从这里继续往上滚
                new_start = max(0, self.local_start_index - 5000)
                if new_start < self.local_start_index:
                    self.local_start_index = new_start
                    self.render_local_page()
                    self.text_edit.verticalScrollBar().setValue(
                        self.text_edit.verticalScrollBar().maximum())
                    moved = True
            if moved and not self._book_veiled():
                # 缓冲区换了位置才存档（每个滚轮刻度都写盘太浪费）
                self.config["last_local_pos"] = self.local_start_index
                self.save_config()
            return
        if not self.current_book:
            return
        if not flip:
            return                          # 滚轮：到头就停，别自动换下一章
        if sb.value() == before:            # 网络书：滚不动了才换章
            if px > 0:
                self.next_chapter()
            else:
                self.prev_chapter()

    def _local_wheel_step(self, delta):
        """滚轮一格（120）= 设置里的行数，和方向键同一套手感。

        以前本地书页的滚轮交给 Qt 默认（固定约 3 行），方向键却整页翻；
        现在两边都走 `scroll_px`，行数在设置里一起调。"""
        lines = clamp_config_int(self.config, "arrow_scroll_lines",
                                 ARROW_SCROLL_LINES, 1, ARROW_SCROLL_MAX)
        self.scroll_px((float(-delta) / 120.0) * lines * self._line_height_px(), flip=False)

    def _feed_hstep(self):
        """左右滑一格走多少像素：和竖直同一套——"滚动行数 × 一行高"。"""
        lines = clamp_config_int(self.config, "arrow_scroll_lines",
                                 ARROW_SCROLL_LINES, 1, ARROW_SCROLL_MAX)
        return max(8, int(round(lines * self._line_height_px())))

    def _feed_hslide(self, step):
        """列多的表（技术统计 17 列）靠左右滑看右边的列。

        **横条不显示**（真人："左右滑不要显示进度条"）：`ScrollBarAlwaysOff`，但范围还在，
        `setValue` 照样有效——所以靠滚轮和左右箭头滑，没有那根可以拖的条。
        Qt 富文本的滚动是整页一个，不会只滑一张表。"""
        if not step:
            return False
        try:
            hbar = self.text_edit.horizontalScrollBar()
            # 页边距会让文档比视口宽几像素（行程是个位数），那不是"能横着滑"：
            # 这种假溢出不抢滚轮、也不抢左右键。
            if hbar.maximum() <= 16:
                return False
            hbar.setValue(max(hbar.minimum(),
                              min(hbar.maximum(), hbar.value() + int(step))))
            return True
        except Exception:
            return False

    def on_peek_exit(self, reason):
        self.exit_peek(reason)

    # --- 安全文档模式：一键把书页换成一份预置的工作文档（有人凑过来看屏幕时用） ---
    def safe_doc_text(self):
        """安全文档正文：设置里指了文件就优先用文件，读不出来一律回落内置版——绝不能露出书页。"""
        path = (self.config.get("safe_doc_path") or "").strip()
        if path:
            try:
                if not os.path.exists(path):
                    raise FileNotFoundError(path)
                if path.lower().endswith(".epub"):
                    content, _chapters, _headings = load_epub_with_headings(path)
                else:
                    try:
                        with open(path, "r", encoding="utf-8-sig") as f:
                            content = f.read()
                    except UnicodeDecodeError:
                        with open(path, "r", encoding="gb18030") as f:
                            content = f.read()
                if content and content.strip():
                    return content
                log_event(f"安全文档内容为空，改用内置版: {path}")
            except Exception as e:
                log_event(f"安全文档读取失败({e})，改用内置版: {path}")
        return SAFE_DOC_TEXT

    def update_tray_tooltip(self):
        try:
            if not hasattr(self, "tray_icon"):
                return
            if self.review_mode:
                # 托盘提示里**不放牌组名**：那些名字（背诵::诗句…）本身就是破绽
                s = self.review_session or {}
                tail = ""
                if s.get("remaining") is not None:
                    tail = f" · 待复习 {s['remaining']}"
                self.tray_icon.setToolTip(f"StealthReader（进程运行中）{tail}")
            else:
                self.tray_icon.setToolTip("StealthReader（安全文档模式 · 进程运行中）" if self.safe_mode
                                          else "StealthReader 阅读器（进程运行中）")
        except Exception:
            pass

    def toggle_safe_mode(self):
        if self.safe_mode:
            self.exit_safe_mode()
        else:
            self.enter_safe_mode()

    def _reset_page_layout(self):
        """把"上一屏留下的排版遗留"清干净：滚动位置、视口留白、页底裁切量。

        为什么必须有这一步：页底裁切量（`page_content_bottom` / 视口下留白）是**按视口算出来的**，
        而算的时候还会读当前的 `viewportMargins().bottom()`——社区页留的是它自己那一套
        （实测 19px），于是书页回来重算时会走另一条分支：退出前页底裁切 = 659px、留白 24px，
        退出后变成 631px、留白 32px，**每进出一次都变一点**，人看到的就是
        "进出社区页之后，阅读的字体和大小被动了"。归零再算就没有这个回路。
        """
        try:
            self.text_edit.verticalScrollBar().setValue(0)
            vm = self.text_edit.viewportMargins()
            if vm.left() or vm.top() or vm.right() or vm.bottom():
                self.text_edit.setViewportMargins(0, 0, 0, 0)
            self.page_content_bottom = self.text_edit.viewport().height()
        except Exception:
            pass

    def snapshot_book_state(self):
        """记下"书现在读到哪儿、正文区显示的是什么"。安全文档与隐形背诵共用这一套。"""
        return {
            "is_local_mode": self.is_local_mode,
            "local_file_path": self.local_file_path,
            "local_full_text": self.local_full_text,          # 字符串引用，不复制，不占内存
            "local_start_index": self.local_start_index,
            # 页内滚到哪一行也要存：只存页首的话，回来会掉回本页最上面（看着就是"上翻"）
            "local_top_offset": self.current_top_offset(),
            "local_page_history": list(self.local_page_history),
            "local_chapters": self.local_chapters,
            "local_heading_offsets": self.local_heading_offsets,
            "current_book": self.current_book,
            "current_chapter_index": self.current_chapter_index,
            "display_text": self.text_edit.toPlainText(),
        }

    def _settle_page_layout(self):
        """等一轮事件循环再钉一次页底。滚动条出现/消失会改变视口宽度，页底裁切量要按新几何重算；
        社区页/卡片页显示时不动——那两页不用书页那套"退到最后一个完整行"的裁切。

        顺带再钉一次字体：这条计时器跑在"退出社区页/安全文档/背诵"之后的那一轮事件循环里，
        是**最后一双会重排书页的手**。谁在这之前把字体换了，这里都会按配置纠正回来。"""
        if self.feed_mode or (self.review_mode and not self.safe_mode):
            return
        self._pin_book_font("页底定稿")
        self.apply_page_margins()

    def restore_book_state(self, snap, write_progress=True):
        """按快照把书页放回来。

        `write_progress=False` 时一个字节都不许写：隐形背诵退出时正文区还是卡片，
        按几何算出来的"锚点"属于卡片，写进 last_local_pos 就把真书的阅读进度改坏了
        （安全文档那条路要写，因为它恢复的就是书自己的位置）。"""
        if not snap:
            return
        # 先从"上一屏"的排版里出来：不清这一层，页底裁切量会一路漂（见 _reset_page_layout）
        self._reset_page_layout()
        if snap.get("is_local_mode") and snap.get("local_full_text"):
            # 直接把快照里的全文和偏移放回去重渲：位置精确到字，也不用重读文件
            self.is_local_mode = True
            self.local_file_path = snap.get("local_file_path", "")
            self.local_full_text = snap.get("local_full_text", "")
            self.local_start_index = int(snap.get("local_start_index", 0))
            self.local_page_history = list(snap.get("local_page_history", []))
            self.local_chapters = snap.get("local_chapters", [])
            self.local_heading_offsets = snap.get("local_heading_offsets", set())
            self.apply_style()   # 只刷样式：此刻正文区还是上一份内容，锚点会被安全阀挡住，不会改进度
            # 视线也回到原处：页首直接放在"快照里视口顶那一行"，页内滚动一并还原。
            # 进度只跟着**书自己的**这一行走；别的文档的坐标（0 之类）绝不写进来。
            self.local_start_index = max(0, min(int(snap.get("local_top_offset", self.local_start_index)),
                                                len(self.local_full_text) - 1))
            self.render_local_page()
            if write_progress:
                self.config["last_local_pos"] = self.local_start_index
        elif snap.get("current_book"):
            # 网络书：按记住的章节重新拉一次，走原来那条渲染路径
            self.is_local_mode = False
            self.displaying_local_text = False
            self.current_book = snap.get("current_book")
            self.current_chapter_index = int(snap.get("current_chapter_index", 0))
            self.apply_style()
            self.fetch_chapter_content(self.current_book["bookUrl"], self.current_chapter_index, False)
        else:
            self.is_local_mode = False
            self.displaying_local_text = False
            self.local_full_text = ""
            self.local_start_index = 0
            self.apply_style()
            self.set_display_text(snap.get("display_text", "") or "")
        # 几何定稿后再钉一次页底（滚动条的有无会改视口宽度）
        QTimer.singleShot(0, self._settle_page_layout)

    def enter_safe_mode(self):
        """切到安全文档：排版沿用当前设置（同一条渲染管线），窗口位置大小不动，
        划线高亮全部藏起来，书的状态先存快照——退出后原位恢复。"""
        if self.safe_mode:
            return
        if self.feed_mode:
            self.exit_feed_mode(reason="safe")   # 三个内容页互斥：先进社区页的先把书还原回来
        if self.review_mode:
            # 从背诵里切过来：**不能拍书的快照**——此刻正文区是卡片，拍下来的是卡片的坐标，
            # 退出安全文档时会拿它去恢复书页，把书的位置弄坏。只记一个"我是从卡片来的"。
            self.safe_snapshot = {"from_review": True}
            self.dismiss_action_panel(cancelled=True)
            self.safe_mode = True
            self.local_full_text = self.safe_doc_text()
            self.local_start_index = 0
            self.local_page_history = []
            self.local_chapters = []
            self.local_heading_offsets = detect_txt_headings(self.local_full_text)
            self.displaying_local_text = True
            self.ghost_veiled = False
            self.apply_style()
            self.render_local_page()
            self.update_tray_tooltip()
            log_event("安全文档模式：已进入（从背诵切过来，卡片先收着）")
            return
        self.safe_snapshot = self.snapshot_book_state()
        self.dismiss_action_panel(cancelled=True)
        self.safe_mode = True
        # 借用本地渲染管线：字体/字号/行距/段间距/页边距/缩进全都沿用，排版和书页一模一样
        self.is_local_mode = True
        self.local_file_path = ""            # 不指向任何书：不存进度、不挂划线
        self.local_full_text = self.safe_doc_text()
        self.local_start_index = 0
        self.local_page_history = []
        self.local_chapters = []
        self.local_heading_offsets = detect_txt_headings(self.local_full_text)
        self.displaying_local_text = True
        self.ghost_veiled = False
        self.apply_style()                   # 先刷样式：别让文字停在上一状态的透明态
        self.render_local_page()
        self.update_tray_tooltip()
        log_event("安全文档模式：已切换（书页已隐藏，排版保持不变）")

    def exit_safe_mode(self):
        if not self.safe_mode:
            return
        self.safe_mode = False
        snap = self.safe_snapshot or {}
        self.safe_snapshot = None
        if snap.get("from_review"):
            # 进来时是从背诵切的：退出去要回到**那张卡**，而不是书（书的快照还在 review_snapshot 里）
            self.restore_review_view()
        else:
            self.restore_book_state(snap, write_progress=True)
        self.update_tray_tooltip()
        log_event("安全文档模式：已退出（内容已恢复）")

    def restore_review_view(self):
        """把正文区换回那张卡（背诵状态一直没动过，重渲即可）。"""
        self.is_local_mode = True
        self.local_file_path = ""
        self.local_full_text = ""          # 仍然不是书：守卫照旧生效
        self.displaying_local_text = True
        self.apply_style()                 # review_mode 还是 True → 会重渲当前卡
        self.review_timer.start()

    # ================= 隐形背诵模式（Anki 卡片当成一份文档） =================

    def _book_veiled(self):
        """正文区此刻显示的是不是"不是书"的东西（安全文档 / Anki 卡片）。

        划线、书签、阅读进度、导出这些**只对真书成立**的操作一律看这个判定。有两处漏判会
        直接损坏数据，必须走这里：
          - apply_highlight_selections：书页的彩色高亮会被画到卡片上（一眼露馅）
          - on_viewport_press / on_viewport_release：在卡片上拖选会把**卡片文字按卡片偏移
            写进这本书的划线存档**（数据被写坏）
        """
        return bool(self.safe_mode or self.review_mode or self.feed_mode)

    # --- 社区统一流（第三个内容页：书 / 卡片 / 社区，三者互斥，换页一律靠快照）---

    def feed_source(self):
        if not self.feed_sources:
            return None
        self.feed_source_i = max(0, min(len(self.feed_sources) - 1, self.feed_source_i))
        return self.feed_sources[self.feed_source_i]

    def feed_source_label(self):
        s = self.feed_source()
        return s.label if s else ""

    def feed_image_dir(self):
        src = self.feed_source()
        if src is None:
            return ""
        try:
            return str(src.image_base() or "")
        except Exception:
            return ""

    def feed_source_note(self):
        """来源自己的"这次少了谁 / 这次是不是旧缓存"提示（页脚说清楚，其余内容照常看）。

        这不是错误弹窗：缺的那一条就是"这个站现在拿不到"，旧缓存那条就是"网又不行了，
        先看上次拿到的"。"""
        src = self.feed_source()
        if src is None:
            return ""
        errs = list(getattr(src, "errors", None) or [])
        if not errs:
            one = getattr(src, "last_error", "")
            errs = [one] if one else []
        # 旧缓存：可能是这个来源自己（单来源页），也可能是混流里某一家（混流把 key 收在 stale 里）
        stale = bool(getattr(src, "served_stale", False))
        keys = set(getattr(src, "stale", None) or [])
        if keys:
            stale = True
        bits = []
        if len(errs) == 1:
            bits.append(errs[0])
        elif len(errs) > 1:
            bits.append("%d 个来源没连上（%s）" % (len(errs), errs[0]))
        if stale:
            bits.append("用的是上次的缓存")
        # "要登录但没配"是一条状态，不是错误：页脚说一句，免得人对着 NGA 那一片空白猜原因。
        if getattr(src, "needs_auth", False) and not getattr(src, "has_auth", False):
            bits.append("这个来源要 Cookie（feed_auth.json）")
        return "；".join(bits)

    def feed_status_text(self):
        if not FEED_OK or self.feed_nav is None:
            return FEED_IMPORT_HINT or "社区统一流不可用"
        err = getattr(self, "feed_error", "")
        if err:
            return f"取数失败：{err}"
        base = feed_mod.status_text(self.feed_nav, self.feed_source_label())
        note = self.feed_source_note()
        return f"{base}｜{note}" if note else base

    def feed_load(self, what, post=None, fresh=False):
        """社区页取数：一律后台线程 + 信号回主线程。

        批次 A 的来源是离线夹具（读本地 JSON），但走的**和真实来源完全同一条路**——
        省得等真网络接上来才发现线程/信号这套接错了地方。

        `fresh=True` 时这一趟**不吃缓存**（人按 R 要的就是"现在这一下"）：列表缓 5 分钟、
        正文缓一天，以前按 R 只是把缓存又读了一遍，屏幕上当然什么都不变——真人报的
        "怎么不支持刷新"就是这个。开关是线程本地的（见 `feed_net.set_fresh`）。"""
        if not FEED_OK or self.feed_nav is None or self.feed_busy:
            return
        nav = self.feed_nav
        src = self.feed_source()
        if src is None:
            return
        if what == "more" and not nav.more:
            return
        if what == "comments" and not nav.c_more:
            return
        # 详情取谁：**先看 nav.post**（详情页当前这一条），没有再退回列表选中项。
        # 只用 nav.selected() 会踩到"详情里再进一层"（知乎回答 → 该问题的全部回答）：
        # 那时列表选中项还是那个回答，于是点标题进了问题页，又被回答的正文盖了回去。
        target = (post or nav.post or nav.selected()) if what == "detail" else None
        if what == "detail" and target is None:
            return
        if what == "comments":
            target = nav.post                  # 评论翻页永远翻"当前这一条"的评论
            if target is None:
                return
        if self.feed_busy:
            # 上一次还没回来。**不能直接丢**：切换来源或者快速连按的时候丢掉一次，
            # 页面就空着（或停在上一家的内容上）而且没有任何机制会再补一次——实测踩到过。
            # 记下来，等这次回来立刻补上。
            self.feed_pending = (what, target)
            return
        channel = nav.channel if what in ("list", "more") else None
        cursor = (nav.cursor if what == "more"
                  else nav.c_cursor if what == "comments" else None)
        tag = getattr(src, "key", "")     # 这次取的是哪一家：回来时要核对
        gen = self.feed_gen               # 这次取的是哪一"代"视图：回来时也要核对
        memo_key = (("list", tag, channel or "", cursor or "") if what in ("list", "more")
                    else ("comments", tag, target.id if target is not None else "", cursor or "")
                    if what == "comments"
                    else ("detail", tag, target.id if target is not None else ""))

        # 快路径：内存里有这一份就先画（0 毫秒），再照旧去取。
        # 这不是"不刷新"——取回来的内容一般一样（缓存命中），一样就不重排；
        # 真变了（新帖子/新评论）才重排。人按 R 时 fresh=True，这条路直接跳过。
        if not fresh and what != "comments":      # 评论翻页只有"下一页"，没有快路径可走
            hit = self._feed_memo.get(memo_key)
            if hit is not None:
                payload0, when0 = hit
                if what in ("list", "more"):
                    posts0, nxt0, append0 = payload0
                    self.on_feed_data("list", (posts0, nxt0, append0, gen, tag))
                else:
                    p0, c0, n0 = payload0
                    self.on_feed_data("detail", (p0, c0, n0, gen, tag))
                if time.time() - when0 < self.FEED_MEMO_TTL:
                    # 刚取过：这一趟连请求都不用发（磁盘 TTL 到没到都不管，按 R 才强制重取）。
                    # 切栏目、退回列表、反复进同一个帖子，全在这一步就是 0 请求。
                    return

        self.feed_busy = True

        def work():
            feed_net.set_fresh(fresh)
            try:
                if what in ("list", "more"):
                    posts, nxt = src.list(channel, cursor)
                    if not fresh:
                        self._feed_memo_put(memo_key, (posts, nxt, what == "more"))
                    self.feed_data_signal.emit("list", (posts, nxt, what == "more", gen, tag))
                elif what == "comments":
                    cmts, _why, nxt = feed_mod.source_comments_page(src, target, cursor)
                    self.feed_data_signal.emit("comments", (cmts, nxt, gen, tag))
                    # 提前渲染：列表刚回来就把第一条的正文/评论预取回来放着。
                else:
                    ev, mine = self._feed_lock(memo_key)
                    if not mine and not fresh:
                        ev.wait(20)      # 预取正在取这一条：等它，别重复发请求
                    try:
                        p, _ = src.detail(target)
                        cmts, _why, cnxt = feed_mod.source_comments_page(src, target, None)
                        if not fresh:
                            self._feed_memo_put(memo_key, (p, cmts, cnxt))
                        self.feed_data_signal.emit("detail", (p, cmts, cnxt, gen, tag))
                    finally:
                        if mine:
                            self._feed_unlock(memo_key, ev)
            except Exception as e:
                try:
                    self.feed_data_signal.emit("error", (str(e), gen, tag))
                except Exception:
                    pass
            finally:
                feed_net.set_fresh(False)


        threading.Thread(target=work, daemon=True).start()

    def _feed_lock(self, key):
        """同一条内容只让一个线程去取。返回 (Event, 我是不是第一个)。"""
        try:
            ev = self._feed_inflight.get(key)
            if ev is None:
                ev = threading.Event()
                self._feed_inflight[key] = ev
                return ev, True
            return ev, False
        except Exception:
            return threading.Event(), True

    def _feed_unlock(self, key, ev):
        try:
            self._feed_inflight.pop(key, None)
            ev.set()
        except Exception:
            pass

    def feed_post_at(self, pos):
        """鼠标底下是列表里的哪一条（不在列表页、或者点的是空白就 None）。"""
        nav = self.feed_nav
        if nav is None or nav.view != "list" or not nav.posts:
            return None
        try:
            block_no = self.text_edit.cursorForPosition(pos).blockNumber()
            hit = None
            for obj, kind, cur in self.feed_item_blocks():
                if cur.blockNumber() <= block_no:
                    hit = (obj, kind)
                else:
                    break
        except Exception:
            return None
        if hit is None or hit[1] != "post":
            return None
        return hit[0]

    def feed_warm_soon(self, post):
        """光标落到某条上：等它停 180ms 再把正文取回来（扫过去的时候不会乱发请求）。"""
        if post is None or not self.feed_mode or not self._feed_warm_enabled():
            return False
        nav = self.feed_nav
        if nav is None or nav.view != "list":
            return False
        if self._feed_warm_post is not None and str(self._feed_warm_post.id) == str(post.id):
            return False
        self._feed_warm_post = post
        self._feed_warm_timer.start()
        return True

    def _feed_warm_enabled(self):
        return True

    def _feed_warm_now(self):
        post = self._feed_warm_post
        self._feed_warm_post = None
        if post is not None:
            self.feed_warm(post)
        return False

    def feed_warm(self, post=None):
        """把一条帖子的正文 + 评论提前取回来放在内存里（人按回车时就是 0 毫秒）。

        和"人按下的那次"共用 `_feed_lock`：同一条只会有一个线程在取，绝不会各发一遍请求。
        """
        nav = self.feed_nav
        if not self.feed_mode or nav is None:
            return False
        if post is None:
            post = nav.selected()
        if post is None:
            return False
        try:
            tag = getattr(self.feed_source(), "key", "")
            key = ("detail", tag, post.id)
            if key in self._feed_memo or key in self._feed_inflight:
                return False
            ev, mine = self._feed_lock(key)
            if not mine:
                return False
            src = self.feed_source()
            gen = self.feed_gen

            def work():
                try:
                    dp, _e = src.detail(post)
                    cm, _c, cnxt = feed_mod.source_comments_page(src, post, None)
                    self._feed_memo_put(key, (dp, cm, cnxt))
                    self.feed_data_signal.emit("prefetch", (dp, cm, cnxt, gen, tag))
                except Exception:
                    pass
                finally:
                    self._feed_unlock(key, ev)

            threading.Thread(target=work, daemon=True).start()
            return True
        except Exception:
            return False

    def feed_warm_top(self, posts=None, n=3):
        """列表回来：串着把前 n 条的正文取回来（一条一条来，别一上来就并发打人家）。

        人看列表的这几秒正好用来预热——等他点的时候前几条已经是 0 毫秒。
        同一条永远只取一次（`_feed_lock`），换来源/换栏目/进详情就立刻收手。
        """
        nav = self.feed_nav
        if not self.feed_mode or nav is None:
            return False
        try:
            posts = list(posts or nav.posts)[:n]
        except Exception:
            return False
        if not posts:
            return False
        src = self.feed_source()
        tag = getattr(src, "key", "")
        gen = self.feed_gen

        def work():
            for p in posts:
                if gen != self.feed_gen or tag != getattr(self.feed_source(), "key", ""):
                    return
                key = ("detail", tag, p.id)
                if key in self._feed_memo:
                    continue
                try:
                    ev, mine = self._feed_lock(key)
                    if not mine:
                        continue
                    try:
                        dp, _e = src.detail(p)
                        cm, _c, cnxt = feed_mod.source_comments_page(src, p, None)
                        self._feed_memo_put(key, (dp, cm, cnxt))
                        self.feed_data_signal.emit("prefetch", (dp, cm, cnxt, gen, tag))
                    finally:
                        self._feed_unlock(key, ev)
                except Exception:
                    continue

        threading.Thread(target=work, daemon=True).start()
        return True

    def _feed_memo_put(self, key, value):
        """存一份到内存（带取到的时间）。只留最近 80 份，再多就丢最旧的。"""
        try:
            self._feed_memo[key] = (value, time.time())
            if len(self._feed_memo) > 80:
                for k in list(self._feed_memo)[:len(self._feed_memo) - 80]:
                    self._feed_memo.pop(k, None)
        except Exception:
            pass

    def _feed_sig_now(self):
        """当前视图的内容签名：一样就不再 setHtml（省掉一次重排，也不让滚动条归位）。"""
        nav = self.feed_nav
        if nav is None:
            return None
        if nav.view == "detail":
            p = nav.post
            roots = nav.roots or []
            return ("detail", getattr(p, "id", ""),
                    len(getattr(p, "body", None) or []),
                    sum(len(b.text or "") for b in (getattr(p, "body", None) or [])),
                    len(roots), sum(c.total() for c in roots))
        posts = nav.posts or []
        return ("list", getattr(nav, "channel", ""), nav.view, len(posts),
                tuple(p.id for p in posts))

    def on_feed_data(self, kind, payload):
        """取数回来了（QueuedConnection 保证这里是主线程）。"""
        self.feed_busy = False
        if kind == "list":
            posts, nxt, append, gen, tag = payload
        elif kind == "detail":
            post, cmts, cnxt, gen, tag = payload
        elif kind == "comments":
            cmts, nxt, gen, tag = payload
        elif kind == "prefetch":
            # 预取只是"提前放在手边"：只有当人此刻正好站在这条帖子的详情页上才算数，
            # 否则安静丢掉（他已经翻到别处了，拿旧数据去改导航会把人家正在看的内容换掉）。
            dp, cm, cnxt, pgen, ptag = payload
            nav0 = self.feed_nav
            if (pgen == self.feed_gen and self.feed_mode and nav0 is not None
                    and nav0.view == "detail" and nav0.post is not None
                    and str(nav0.post.id) == str(dp.id)
                    and ptag == getattr(self.feed_source(), "key", "")):
                self.on_feed_data("detail", (dp, cm, cnxt, pgen, ptag))
            return
        elif kind == "boards":
            boards, gen, tag = payload
        else:
            msg, gen, tag = payload
        # 两道核对，缺一不可：
        #   来源对不上 = 这是上一家的结果（切换来源时旧请求还在飞，一定会发生）；
        #   代次对不上 = 结果回来之前视图已经换了（打开详情/退回列表/重新拉取），
        #                拿它去改导航状态就会把人家刚打开的详情冲掉、或者把视图弹回详情。
        gone = gen != self.feed_gen or (bool(tag) and tag != getattr(self.feed_source(), "key", ""))
        if not self.feed_mode:
            self.feed_pending = None
            return                                  # 人已经切走了，结果和排队一起丢
        if kind == "boards":
            # 板块表回来 → 弹可搜索的选择器。它不改视图，所以只核对来源（代次变了也要丢：
            # 那说明中途换了来源/栏目，此刻弹出来的列表已经对不上了）
            pending = self.feed_board_pending
            self.feed_board_pending = False
            log_event("社区：板块表拿到 %d 条（来源 %s，还用得上=%s）"
                      % (len(boards or []), tag, pending and not gone))
            if gone or not pending:
                return
            self.feed_board_picker()
            return
        if gone:
            pend = self.feed_pending
            self.feed_pending = None
            if pend is not None:
                self.feed_load(pend[0], pend[1])    # 排队的那次优先，别把人家的回车吃掉
            elif self.feed_nav.stale:
                self.feed_load("detail" if self.feed_nav.view == "detail" else "list")
            return
        if kind == "error":
            self.feed_error = str(msg)
            log_event(f"社区取数失败: {msg}")
            if self._feed_live_watching():
                # 5 秒一拍的重取失败了：退让，别每 5 秒撞一次站点。
                # 成功的那一拍会把它清零（见下面的 detail 分支）。
                self._feed_live_fail = min(4, int(self._feed_live_fail) + 1)
                _gap = min(30, 2 ** (self._feed_live_fail + 1))
                self._feed_live_retry_at = time.time() + _gap
                log_event("直播重取失败（第 %d 次），%d 秒后再试"
                          % (self._feed_live_fail, _gap))
            if self.feed_board_pending:              # 拉板块表失败：这不该让整页变成"取数失败"
                self.feed_board_pending = False
                self.feed_error = ""
                self.review_hint("板块列表没拉到（%s）" % str(msg)[:40], 2000)
        else:
            self.feed_error = ""
            nav = self.feed_nav
            if kind == "list":
                if not append:
                    nav.reset()
                    nav.add_page(posts, nxt)
                    if nav.posts:
                        self.feed_warm_top(nav.posts)    # 顺手把前三条的正文取回来放着
                else:
                    nav.add_page(posts, nxt)
                # 新一页到了：重新武装"到底自动续页"（每次到底只发一次）
                self.feed_more_armed = True
            elif kind == "detail":
                nav.set_detail(post, cmts, cnxt)
                self._feed_live_fail = 0        # 这一拍成了：退让清零
                self._feed_live_retry_at = 0.0
            elif kind == "comments":
                nav.add_comments(cmts, nxt)
                self.feed_more_armed = True        # 新一页到了：重新武装"滚到底续页"
            if self._feed_sig_now() != self._feed_sig:
                self.render_feed()
            if kind == "detail" and self.feed_detail_top:
                # 正文/评论到齐、版面变高了：从顶部读起（不是停在第一条评论上）
                self.feed_detail_top = False
                self.feed_scroll_top()
        # 排队的那次现在补上（切来源、快速按键都靠它才不丢内容）
        pend = self.feed_pending
        self.feed_pending = None
        if pend is not None:
            self.feed_load(pend[0], pend[1])
        self.update_review_bar()

    def render_feed(self):
        """社区页唯一的渲染入口：列表和详情都从这里出去，排版统一由 apply_feed_style 兜。"""
        if not FEED_OK or self.feed_nav is None:
            return
        nav = self.feed_nav
        theme = self.feed_theme()
        if nav.view == "detail":
            html = feed_mod.render_detail(nav.post, nav, theme)
        else:
            html = feed_mod.render_list(nav.posts, nav, theme)
        before = None
        try:
            before = int(self.text_edit.verticalScrollBar().value())
        except Exception:
            before = None
        try:
            h_before = int(self.text_edit.horizontalScrollBar().value())
        except Exception:
            h_before = 0
        doc = self.text_edit.document()
        doc.setDefaultStyleSheet("")
        base = self.feed_image_dir()
        if base:
            doc.setBaseUrl(QUrl.fromLocalFile(base + os.sep))
        self.text_edit.setHtml(html)
        if base:
            doc.setBaseUrl(QUrl.fromLocalFile(base + os.sep))   # setHtml 之后再钉一次（同卡片那套）
        self.apply_feed_style()
        self.feed_highlight()
        self.update_review_bar()
        self._feed_sig = self._feed_sig_now()
        want = self.feed_restore_pos
        self.feed_restore_pos = None
        same_screen = want is None
        if want is None:
            # 同一屏重画（到底续了一页、追加评论、摊开子回复）：位置**不能动**。
            # `setHtml` 一律把滚动条打回 0，以前只在列表里补这一下，详情里就没人管——
            # 于是读评论时往下翻一页，屏幕直接跳回顶上（人报的"下翻加载评论会跳屏到
            # 评论区第一条"）。要"从头读"的地方都显式给了 `feed_restore_pos = 0`。
            want = before
        # 横向只在"同一屏重画"时保留：换了帖子/切了视图就回最左边。
        # （不保留的话，比赛进行中每 5 秒静默重画一次，正在看的右边那几列会被拉回最左）
        self._feed_h_before = h_before if same_screen else 0
        if want or self._feed_h_before:
            # 文档刚换完，高度还没算出来，得等这一轮事件走完再滚；期间要是又有人
            # 明确要求"回顶部"（feed_scroll_top），这次恢复就作废，别把它拽回去。
            self._feed_pending_scroll = want
            QTimer.singleShot(0, self._feed_apply_pending_scroll)
        else:
            self._feed_pending_scroll = None      # 这一次明确不要恢复，别让上一次的排单生效
        if getattr(self, "feed_live_timer", None) is not None:
            self.feed_live_watch()

    def _feed_live_watching(self):
        """此刻是不是"正在看一场还没打完的球"（定时重取、失败退让都看这一条）。"""
        nav = self.feed_nav
        post = nav.post if nav is not None else None
        return bool(self.feed_mode and nav is not None and nav.view == "detail"
                    and post is not None and "进行中" in (post.tags or []))

    def feed_live_watch(self):
        """按当前视图决定直播那张表要不要走：进行中的比赛详情 → 开；其它情况 → 停。"""
        timer = getattr(self, "feed_live_timer", None)
        if timer is None:
            return
        if self._feed_live_watching():
            if not timer.isActive():
                timer.start()
        elif timer.isActive():
            timer.stop()

    def feed_live_tick(self):
        """5 秒到点：还在看一场没结束的球，就静默重取这一条（位置由 render_feed 保）。

        5 秒一拍是真人定的（先说 2 秒，看下来觉得太密，改成 5 秒）。这么密得自己兜住两件事：

        1. 上一拍还没回来（`feed_busy`）就**跳过**这一拍——绝不把请求排队堆起来；
        2. 连着失败就退让（2→4→8→16→30 秒封顶，成功一拍立刻清零），
           否则站点一限流，就是每 2 秒撞一次，屏幕上还尽是"取数失败"。

        "每 5 秒"不等于"每 5 秒抖一下"：内容没变时连重排都不会发生
        （`_feed_sig_now` 比签名，见 on_feed_data），真出现了新回合才重画，
        滚动位置仍由 render_feed 接着。
        """
        if not self._feed_live_watching():
            self.feed_live_timer.stop()
            self._feed_live_fail = 0
            self._feed_live_retry_at = 0.0
            return
        if time.time() < float(self._feed_live_retry_at or 0):
            return                    # 刚才连着失败：退让还没到点，这一拍不发请求
        if self.feed_busy:
            return                    # 上一次还没回来：这一拍跳过，下一拍再说
        self.feed_load("detail", fresh=True)

    def feed_toggle_view(self):
        """文字直播 ↔ 数据统计（只有这一场支持切换的来源才动）。

        切完必须把内存里那份旧详情**扔掉**：`feed_load` 的快路径按 (detail, 来源, id) 取，
        不扔的话按下 T 只是把刚看过的那份又画一遍，看起来像"没反应"。"""
        nav = self.feed_nav
        if not self.feed_mode or nav is None or nav.view != "detail" or nav.post is None:
            return False
        src = self.feed_source()
        fn = getattr(src, "toggle_detail_view", None)
        if fn is None:
            return False
        try:
            view = fn(nav.post)
        except Exception as exc:
            log_event("切换文字直播/数据统计失败：%s" % exc)
            return False
        self._feed_memo.pop(("detail", getattr(src, "key", ""), nav.post.id), None)
        self.feed_load("detail")
        log_event("这一场切到：%s" % ("数据统计" if view == "stats" else "文字直播"))
        return True

    def feed_view_label(self):
        """菜单上写"现在能切到哪儿"。返回空串 = 这一页没有可切的。"""
        nav = self.feed_nav
        if not self.feed_mode or nav is None or nav.view != "detail" or nav.post is None:
            return ""
        src = self.feed_source()
        ask = getattr(src, "detail_view", None)
        if ask is None or getattr(src, "toggle_detail_view", None) is None:
            return ""
        try:
            view = ask(nav.post)
        except Exception:
            return ""
        return "📝 看文字直播（T）" if view == "stats" else "📊 看数据统计（T）"

    def apply_feed_style(self):
        """社区页排版：左对齐、不首行缩进、行距沿用阅读设置；卡片间距和评论缩进由 HTML 自带。

        字体用 `feed_font()`（默认跟书页一样，可单独设），**不能改坏书页**：这里只动
        text_edit 的字体和文档格式，书页回来时会整套重排（见 restore_book_state / _reset_page_layout）。

        **只 merge 这三样，绝不碰 block 的 margin**：`mergeBlockFormat` 会把 modifier 里
        显式设过的属性盖进每个块，一旦这里 setTopMargin(0)/setBottomMargin(0)，
        HTML 里卡片之间、评论之间的间距会被整片抹平，一屏挤成一大坨。
        （卡片页那张卡只有一块内容，所以那边可以这么写，这里不行。）"""
        self.text_edit.setFont(self.feed_font())
        bf = QTextBlockFormat()
        bf.setLineHeight(max(100, int(self.config.get("line_spacing", 160))),
                         QTextBlockFormat.ProportionalHeight)
        bf.setAlignment(Qt.AlignLeft)
        bf.setTextIndent(0)
        cur = QTextCursor(self.text_edit.document())
        cur.select(QTextCursor.Document)
        cur.mergeBlockFormat(bf)
        self.apply_page_margins(cut=False)   # 社区流是滚动的，不做书页那种"退到整行"的底部裁切

    def feed_text_color(self):
        """社区页要用的正文颜色 = **正文此刻实际在用的颜色**。

        透明窗口下这个值不能只看 config 里的初始值：auto（变色龙）模式会把正文换成纯黑或
        纯白（按背景亮度定），幽灵模式还带透明度——浅灰字压在浅色背景上就是"看不清"。
        取值顺序：正文控件当前调色板 → auto 模式按背景亮度算黑/白 → config.text_color。
        """
        col = None
        try:
            col = self.text_edit.palette().color(QPalette.Text)
            if (not col.isValid()) or col.alpha() == 0:
                col = None
        except Exception:
            col = None
        if col is None and self.config.get("auto_mode"):
            bg = getattr(self.content_frame, "auto_bg_fill", None)
            if bg is not None and bg.isValid():
                bright = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
                col = QColor(0, 0, 0) if bright > 128 else QColor(255, 255, 255)
                col.setAlpha(int(self.config.get("opacity", 0.9) * 255))
        if col is None:
            col = QColor(str(self.config.get("text_color") or ""))
        if not col.isValid():
            col = QColor(216, 216, 216)
        return f"rgba({col.red()}, {col.green()}, {col.blue()}, {col.alpha()})"

    def feed_font(self):
        """社区页自己的字体：**没配就是界面无衬线字体栈 + 15px**，不再跟着书页走。

        以前没配就完全跟书页：书页为了读长文会把字号调到 14pt 上下、字体常是宋体，
        搬到列表上就是"每屏两条巨字"，真人原话是"帖子太难看"。社区页是浏览界面，
        和正文本来就不是一类东西，所以各用各的默认值。

        两个键各自独立：只配字号就只换字号，字体用界面栈。取值夹在 11~22，
        免得 config.json 里手写个 0 或 999 把页面弄成一行一个字。
        """
        fam = str(self.config.get("feed_font_family") or "").strip() or "Microsoft YaHei UI"
        try:
            size = int(self.config.get("feed_font_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if not 11 <= size <= 22:
            size = getattr(feed_mod, "FEED_SIZE_DEFAULT", 15)
        return QFont(fam, size)

    def feed_theme(self):
        # 主题自己去读 `feed_font_size` / `feed_font_family`（见 feed.theme_from_config），
        # 这里只把**正文此刻实际的颜色**递进去：auto/幽灵模式会换色，靠 config 猜就不准了。
        theme = feed_mod.theme_from_config(dict(self.config or {}),
                                           max(240, self.width() - 40),
                                           text_color=self.feed_text_color())
        # 这一页是不是"某个回答"，标题（= 那个问题）能不能点进去看全部回答
        theme["title_link"] = bool(self.feed_question_id())
        return theme

    def feed_item_blocks(self):
        """把"每条一段定位文本"落成文档里的光标，按文档顺序：[(对象, 类型, 光标)]。

        **顺序查找**（每次从上一处接着往下找）而不是全文 find：两条同名作者、同一时间的
        评论表头一模一样，全文 find 永远只命中最早那条——高亮就会一直粘在它上面。
        鼠标点选也吃这份序列，于是"哪一条在哪儿"只有一份答案。
        """
        if not FEED_OK or self.feed_nav is None:
            return []
        doc = self.text_edit.document()
        out = []
        cur = QTextCursor(doc)
        cur.movePosition(QTextCursor.Start)
        for obj, kind, needle in feed_mod.item_needles(self.feed_nav):
            if not needle:
                continue
            hit = doc.find(needle, cur)
            if hit.isNull():
                # 兜一层：来源文本里的空白可能被 Qt 折成半角空格（`　`→` `）
                flat = " ".join(needle.split())
                if flat and flat != needle:
                    hit = doc.find(flat, cur)
            if hit.isNull():
                continue
            out.append((obj, kind, hit))
            cur = hit
        return out

    def feed_current_block(self):
        nav = self.feed_nav
        if nav is None:
            return None
        target = nav.current_comment() if nav.view == "detail" else nav.selected()
        if target is None:
            return None
        for obj, _kind, cur in self.feed_item_blocks():
            if obj is target:
                return cur
        return None

    def feed_click(self, pos, force=False):
        """鼠标点在社区页上：**点一下就到**——列表里点帖子就进去，详情里点评论就折叠/展开。

        以前详情的写法是"第一次只换选中、再点同一条才折叠"，真人一句话点破：
        "折叠和展开都需要点两次，一次就好了"。这套双击语义是从 reddit 网页搬来的，
        但这个窗口不是网页：选中是一个看不见的状态（只有字色变一点），人点一下没反应
        只会再点一次，还以为是坏了。现在点评论 = 折叠/展开（顺带把它变成当前条目）。

        这里以前是"单击只换选中、再单击才进去"，reddit 网页确实是这样，但在这个窗口里
        它是错的：列表本身就是滚动的一整页，单击换选中会顺手把页面滚一下（见 feed_ensure_visible），
        人的感受是"点一下没反应、还跳了一下，得再点一次"——所以列表改成点一下就到。

        点在空白上不动光标；点正文里的字仍然是"选字复制"（走原来的划选路径，见 on_viewport_release）。
        """
        nav = self.feed_nav
        if nav is None:
            return False
        if getattr(self, "_feed_press_had_sel", False):
            self._feed_press_had_sel = False
            return False        # 先把字划上了再点：那是选字复制，不是折叠/进帖
        block_no = self.text_edit.cursorForPosition(pos).blockNumber()
        if nav.view == "detail" and block_no == 0 and self.feed_question_id():
            # 详情页点标题 = 进这个问题的全部回答（标题就是问题）
            return self.feed_open_question()
        hit = None
        for obj, kind, cur in self.feed_item_blocks():
            if cur.blockNumber() <= block_no:
                hit = (obj, kind)
            else:
                break
        if hit is None:
            return False
        obj, kind = hit
        cur_sel = nav.current_comment() if nav.view == "detail" else nav.selected()
        # 先把这个条目变成当前条目（双击可能点的就是别的条目）
        if kind == "comment":
            for i, (c, _d, _p) in enumerate(nav.visible_comments()):
                # 按 **id** 比，不能按对象身份：`visible_comments()` 里的"还有 N 条回复"
                # 那一行是每次现造的（cid 才是稳定的），`is` 永远不成立 → 点上去等于没选中，
                # 展开/收起就落到上一条身上。人报的"更多子评论点了没反应"就是这个。
                if c is obj or c.id == getattr(obj, "id", None):
                    nav.c_index = i
                    break
        else:
            try:
                nav.index = nav.posts.index(obj)
            except ValueError:
                return False
        if kind == "comment":
            # 评论：点一下就是折叠/展开（"一次就好了"）。光标已经在别的条目上也一样——
            # `feed_click` 开头已经把被点的那条设成了当前条目。
            return self.feed_toggle_collapse()
        # 列表：一下就到（force 也就是"双击"，走同一条路，不重复动作）
        return self.feed_open()

    def feed_highlight(self, ensure=False):
        """把选中项提亮一档（extra selections，只改字色，不铺底色块）。

        `ensure` 只有"人主动换条目"时才给（方向键、点选）：那时候把目标拉进可见区是用户
        想要的。**渲染（render_feed）一律不带它**——进帖子时第一条评论天然是"当前条目"，
        以前这里无条件 ensure，于是刚点进帖子就被滚到评论区开头（真人原话：
        "点进帖子应该在顶部，现在是直接在评论区开头"）。

        定位文本由 feed.selection_needle 给（列表是标题、评论是表头那一行），每动一次
        重算一次——所以高亮永远跟着光标走，不依赖上一次渲染出来的标记。

        **只把目标拉进可见区，不主动改滚动位置**：以前这里是无条件 feed_scroll_to，
        于是"点一下帖子"整页跟着跳（实测点第 7 条时滚动值从 1017 跳到 1187），
        看着就像"点击的定位逻辑坏了"。"""
        try:
            nav = self.feed_nav
            if nav is None:
                self.text_edit.setExtraSelections([])
                return
            theme = self.feed_theme()
            cur = self.feed_current_block()
            if cur is None:
                self.text_edit.setExtraSelections([])
                return
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cur
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(theme["mark"]))
            sel.format = fmt
            self.text_edit.setExtraSelections([sel])
            if ensure:
                self.feed_ensure_visible(cur)
        except Exception:
            pass

    def feed_ensure_visible(self, cursor):
        """目标已经在"看得舒服"的区间里就一个像素都不动；不在才滚到视口 25% 处。"""
        try:
            rect = self.text_edit.cursorRect(cursor)
            vh = max(1, self.text_edit.viewport().height())
            if int(vh * 0.12) <= rect.top() and rect.bottom() <= int(vh * 0.88):
                return False
            return self.feed_scroll_to(cursor)
        except Exception:
            return False

    def feed_scroll_to(self, cursor):
        """把目标滚到视口靠上的位置。

        不移动插入点：只读页面里留一个闪烁光标，一眼就不像文档了。"""
        try:
            rect = self.text_edit.cursorRect(cursor)
            sb = self.text_edit.verticalScrollBar()
            vh = max(1, self.text_edit.viewport().height())
            target = sb.value() + rect.top() - int(vh * 0.25)
            sb.setValue(max(sb.minimum(), min(sb.maximum(), target)))
            return True
        except Exception:
            return False

    def feed_scroll_top(self):
        """换了一屏内容（进详情/切来源/切栏目）就回到最上面——不然会停在上一屏的位置。

        **不要**往 `feed_restore_pos` 里写 0：它表示"下一次重画要落到哪"，而这里通常
        没有"下一次重画"来消费它，0 就会一直留着，把后面那次"保持位置"的重画（详情里
        追加评论）也改成"不恢复"——`setHtml` 打回的 0 就留在屏幕上了（跳屏那类毛病）。
        取消排队 + 立刻 setValue(0) 已经够了。
        """
        self._feed_pending_scroll = None    # 排队中的"恢复旧位置"作废：现在就要顶部
        try:
            self.text_edit.verticalScrollBar().setValue(0)
        except Exception:
            pass

    def _feed_pos_key(self):
        return (str(getattr(self.feed_source(), "key", "")),
                str(getattr(self.feed_nav, "channel", "") or ""))

    def _feed_save_pos(self):
        """把列表此刻滚到哪记下来（按来源+板块分）。详情页不记：它是另一个页面。"""
        nav = self.feed_nav
        if nav is None or nav.view != "list" or not self.feed_mode:
            return
        try:
            self.feed_list_pos[self._feed_pos_key()] = int(
                self.text_edit.verticalScrollBar().value())
        except Exception:
            pass

    def _feed_apply_pending_scroll(self):
        want = self._feed_pending_scroll
        self._feed_pending_scroll = None
        if want:
            self._feed_set_scroll(want)
        h = getattr(self, "_feed_h_before", 0)
        if h:
            try:
                hbar = self.text_edit.horizontalScrollBar()
                hbar.setValue(max(hbar.minimum(), min(hbar.maximum(), int(h))))
            except Exception:
                pass

    def _feed_set_scroll(self, value):
        try:
            sb = self.text_edit.verticalScrollBar()
            sb.setValue(max(sb.minimum(), min(sb.maximum(), int(value))))
        except Exception:
            pass

    def _feed_from_answer(self):
        """当前详情是不是"从某个回答点标题进来的问题页"。"""
        nav = self.feed_nav
        return bool(nav is not None and nav.stack_via and nav.stack_via[-1] == "question_from_answer")

    def feed_home(self):
        """回列表主页：详情里不管压了几层都退到底，并回到刚才读到的那一行。"""
        nav = self.feed_nav
        if nav is None or nav.view != "detail":
            return False
        if not nav.home():
            return False
        self.feed_gen += 1                  # 还在飞的详情结果不许再把视图弹回详情
        self.feed_restore_pos = self.feed_list_pos.get(self._feed_pos_key())
        self.render_feed()
        self.update_review_bar()
        return True

    def feed_scroll_comments_top(self):
        """E：跳到评论区顶部（第一条评论的表头那一行）。"""
        nav = self.feed_nav
        if nav is None or nav.view != "detail":
            return False
        vis = nav.visible_comments()
        if not vis:
            sb = self.text_edit.verticalScrollBar()
            sb.setValue(sb.maximum())        # 没有评论就停在正文末尾
            return True
        needle = feed_mod.comment_needle(vis[0][0], nav)
        if not needle:
            return False
        doc = self.text_edit.document()
        cur = QTextCursor(doc)
        cur.movePosition(QTextCursor.Start)
        hit = doc.find(needle, cur)
        if hit.isNull():
            return False
        return self.feed_scroll_to(hit)

    def _feed_switch_mark(self, kind):
        """记下"切换之前"的样子：来源下标、板块、以及列表滚到哪（Z 撤回用）。"""
        self._feed_save_pos()          # 现在是旧来源/旧栏目，位置要记在它们头上
        self.feed_switch_prev = {
            "kind": kind,
            "source_i": self.feed_source_i,
            "channel": str(getattr(self.feed_nav, "channel", "") or ""),
            "pos": self.feed_list_pos.get(self._feed_pos_key()),
        }

    def feed_undo_switch(self):
        """Z：撤回上一次切社区/切板块（回到切换前那个来源、那个板块、那个位置）。"""
        nav = self.feed_nav
        prev = self.feed_switch_prev
        if nav is None:
            return False
        if not prev:
            self.review_hint("没有可撤回的切换", 1200)
            return False
        self.feed_switch_prev = None
        changed = False
        if prev["source_i"] != self.feed_source_i and self.feed_sources:
            i = max(0, min(len(self.feed_sources) - 1, int(prev["source_i"])))
            self.feed_source_i = i
            self.config["feed_source"] = getattr(self.feed_source(), "key", "")
            changed = True
        channel = prev.get("channel") or ""
        if channel and channel != nav.channel:
            nav.channel = channel
            self.config[self.feed_board_key()] = channel
            changed = True
        if not changed:
            return False
        self.feed_error = ""
        self.feed_gen += 1
        nav.reset()
        nav.channel = channel or nav.channel or self.feed_first_channel()
        src = self.feed_source()
        self.feed_restore_pos = prev.get("pos")
        self.render_feed()
        self.update_review_bar()
        self.feed_load("list")
        log_event("社区：撤回到 %s · %s" % (getattr(src, "label", ""), nav.channel))
        return True

    def feed_autoload_more(self, *_args):
        """列表页滚到底（离底 60px 以内）就自动续下一页。

        以前只有"选中项快到底"（`nav.near_end()`，得按 ↓ 挪光标）才续页，用滚轮往下读的话
        永远读不到第二页——真人原话"页面下滑到底了继续加载"。取数在后台线程，
        界面不等；`feed_more_armed` 保证一次到底只发一次，翻页回来后重新武装。"""
        nav = self.feed_nav
        if nav is None or not self.feed_mode or self.feed_busy or not self.feed_more_armed:
            return
        sb = self.text_edit.verticalScrollBar()
        if sb.maximum() <= 0 or sb.value() < sb.maximum() - 60:
            return
        if nav.view == "list":
            if not nav.more:
                return
            self.feed_more_armed = False
            self.feed_load("more")
            return
        # 详情页：先把手里**已经取回来**的评论摆出来（0 请求），手里没有了才去取下一页
        if nav.show_more_comments():
            self.render_feed()
            return
        if nav.c_more:
            self.feed_more_armed = False
            self.feed_load("comments")

    def feed_move(self, step):
        nav = self.feed_nav
        if nav is None:
            return False
        moved = nav.move_comment(step) if nav.view == "detail" else nav.move(step)
        if not moved:
            return False
        self.feed_highlight(ensure=True)
        self.update_review_bar()
        if nav.view == "list":
            self.feed_warm_soon(nav.selected())      # 光标停在哪条，就把哪条先取回来
        if nav.view == "list" and nav.near_end():
            self.feed_load("more")      # 快到底就续下一页（取数在后台，界面不等）
        return True

    def feed_open(self):
        nav = self.feed_nav
        if nav is None:
            return False
        if nav.view == "detail":
            # 详情里回车 = 当前这条是"还有 N 条回复"就摊开（其它条目回车不做事，
            # 免得把正在读的评论折叠掉）
            cur = nav.current_comment()
            if getattr(cur, "more_for", ""):
                return self.feed_toggle_collapse()
            return False
        if not nav.open_selected():
            return False
        self.feed_gen += 1                  # 视图换代：还在飞的列表结果不许再盖回来
        self.feed_detail_top = True     # 正文回来时再钉一次顶部（标题先画，正文随后到）
        self.render_feed()              # 先把标题画出来，正文和评论随后台取数回来
        self.feed_scroll_top()          # 换了一屏内容就从最上面读起（不是停在评论那一行）
        self.feed_load("detail")
        return True

    def feed_back(self):
        nav = self.feed_nav
        if nav is None or not nav.back():
            return False
        self.feed_gen += 1                  # 退回列表了，还在飞的详情结果不许把视图又弹回详情
        self.feed_restore_pos = 0           # 换了一层：从顶上读（列表会再 ensure 到刚才那条）
        if getattr(self, "feed_live_timer", None) is not None:
            self.feed_live_timer.stop()     # 回到列表就没有"进行中"这回事了
        self.render_feed()
        cur = self.feed_current_block()
        if cur is not None:
            self.feed_ensure_visible(cur)   # 回到刚才读的那一条（不在可见区里才滚）
        else:
            self.feed_scroll_top()
        return True

    def feed_question_id(self, post=None):
        """当前详情页是"某个问题的某个回答"时，返回那个问题的 id；否则空串。

        知乎的推荐流里既有问题卡片（id 是数字），也有"某问题的某个回答"（id 是 a…）。
        回答页只该看到**这一个回答**和它的评论；想看这个问题下的其它回答，
        点标题（标题就是问题）进去——所以这里得知道它属于哪个问题。
        """
        nav = self.feed_nav
        if post is None:
            if nav is None or nav.view != "detail":
                return ""
            post = nav.post
        if post is None or post.source != "zhihu":
            return ""
        m = re.search(r"/question/(\d+)", post.url or "")
        if not m:
            return ""
        qid = m.group(1)
        return "" if str(post.id) == qid else qid      # 已经在问题页了，就别再"跳"

    def feed_hover_title(self, pos):
        """鼠标是不是停在详情页标题那一行（只有能跳的时候才认）。"""
        nav = self.feed_nav
        if not self.feed_mode or nav is None or nav.view != "detail":
            return False
        if not self.feed_question_id():
            return False
        try:
            return self.text_edit.cursorForPosition(pos).blockNumber() == 0
        except Exception:
            return False

    def feed_open_question(self):
        """知乎回答页点标题：进这个问题的「全部回答」那一页。

        问题页就是"id 是数字的帖子"：正文是问题说明（接口给不出就空着，不编），
        「评论」那一栏列的是**回答**，每条回答底下再挂它自己的评论——三层都在一页里。
        退格原样回到刚才那个回答（`FeedNav.stack`），不重新取数、也不丢折叠状态。
        """
        nav = self.feed_nav
        qid = self.feed_question_id()
        if nav is None or not qid or nav.view != "detail" or nav.post is None:
            return False
        cur = nav.post
        qp = feed_mod.Post(
            source="zhihu", source_label=cur.source_label or "知乎", channel=cur.channel,
            pid=qid, title=cur.title, url="https://www.zhihu.com/question/%s" % qid,
            score=cur.score, score_label=cur.score_label, comment_label="回答",
        )
        if not nav.open_detail(qp, via="question_from_answer"):
            return False
        self.feed_gen += 1
        self.render_feed()
        self.feed_scroll_top()
        self.feed_load("detail")
        return True

    def _feed_comment_y(self, comment):
        """这条评论的表头在**视口里的高度**（找不到就 None）。"""
        if comment is None:
            return None
        try:
            for obj, kind, cur in self.feed_item_blocks():
                if kind == "comment" and obj is comment:
                    return self.text_edit.cursorRect(cur).top()
        except Exception:
            return None
        return None

    def feed_toggle_collapse(self):
        """折叠/展开当前这条评论，**屏幕上的位置不动**。

        折叠会把这条评论的正文和整棵子回复从文档里拿掉，文档一下子变短；重画之后
        `feed_highlight` 又会把目标拉进可见区（不在 12%~88% 那一段才滚，滚到 25% 处），
        于是人点一下"收起"，整页往上/往下跳一截——真人报的就是这个。
        现在先记下这条评论表头此刻的屏幕高度，重画完再按差值把滚动条挪回去：
        被折叠的那一条留在原地，其余内容在它下面收起/展开。
        """
        nav = self.feed_nav
        if nav is None or nav.view != "detail":
            return False
        target = nav.current_comment()
        if getattr(target, "more_for", ""):
            # 这行是"还有 N 条回复"：回车/点击 = 摊开下一批（数据就在手边，不发请求）
            if nav.show_more_children(target.more_for):
                self.render_feed()
            return True
        y_before = self._feed_comment_y(target)
        if not nav.toggle_collapse():
            return False
        # 折叠/展开要**按差值**把这条留在原地（下面那段算术），所以这里明确不要
        # render_feed 的"恢复原像素位置"——两套补偿叠在一起会互相打架。
        self.feed_restore_pos = 0
        self.render_feed()              # 折叠改变了文档结构，只能重画
        y_after = self._feed_comment_y(target)
        if y_before is not None and y_after is not None and y_after != y_before:
            try:
                sb = self.text_edit.verticalScrollBar()
                sb.setValue(int(max(sb.minimum(), min(sb.maximum(),
                                                    sb.value() + (y_after - y_before)))))
            except Exception:
                pass
        return True

    def feed_reload(self):
        """R：重新拉一次，**这一次不吃缓存**（列表 5 分钟、正文一天，吃缓存等于没刷新）。

        列表和详情都支持；在详情里刷新是重新取这一篇的正文和评论，取回来仍停在详情。
        按下去先给一句反馈，免得人以为键没生效（取数本身在后台线程，几百毫秒到几秒）。
        """
        nav = self.feed_nav
        if nav is None:
            return False
        self.feed_error = ""
        self.feed_gen += 1
        what = "detail" if nav.view == "detail" else "list"
        self.review_hint("刷新中…", 900)
        self.feed_load(what, fresh=True)
        log_event("社区：刷新（%s，不吃缓存）" % what)
        return True

    def feed_board_key(self):
        """这个来源的"板块/栏目"记在 config 的哪个键上（每个来源各记各的）。

        以前栏目只活在内存里：切来源再切回来就掉回第一栏。NGA 的板块更是"上次看的那个版
        下次还想接着看"，所以按来源分开记（`feed_board_nga` / `feed_board_v2ex` …）。"""
        src = self.feed_source()
        return "feed_board_%s" % getattr(src, "key", "") if src is not None else ""

    def feed_first_channel(self):
        """进来源/切来源时把栏目放到一个**真实存在**的值上。

        顺序是：config 里这个来源上次停的那一栏（还在列表里就用它）→ 第一栏 → "全部"。
        只用第一栏的话，页脚会显示"全部"而这个来源根本没有"全部"这一栏；
        只认 config 的话，来源改了自己的栏目表之后会停在一个不存在的栏目上（两者来源自己说了算，
        见 channels）。"""
        src = self.feed_source()
        if src is None:
            return "全部"
        try:
            chans = list(src.channels() or [])
        except Exception:
            chans = []
        remembered = str(self.config.get(self.feed_board_key()) or "")
        if remembered:
            if remembered in chans:
                return remembered
            # 记住的那一栏不在 channels() 里，但来源自己认（虎扑手填过 slug、NGA 改过版面表）：
            # 照样用它——"上次看的是哪一栏，下次还回哪一栏"比"必须在本表里"更重要。
            check = getattr(src, "accepts_channel", None)
            if callable(check):
                try:
                    if check(remembered):
                        # 来源可以把老名字归一化成现在的栏目（虎扑把"文字直播/数据统计"
                        # 两栏合成了一栏「NBA 赛程」）——直接返回旧名字，页脚会写着旧栏目
                        # 却显示另一栏的帖子。
                        norm = getattr(src, "normalize_channel", None)
                        if callable(norm):
                            try:
                                return norm(remembered)
                            except Exception:
                                pass
                        return remembered
                except Exception:
                    pass
        return chans[0] if chans else "全部"

    def feed_cycle_source(self, step):
        """`[` `]` 切来源：全部来源（混流）→ IT之家 → V2EX → linux.do → 知乎 → 离线示例。

        批次 B 之后"来源"才真的成了可切的一维：默认落在混流上（真人要的是"一个窗口看所有站"），
        想只看某一个站就按 `]`；栏目是来源内部的再一层，用 `,` `.` 切（见 feed_cycle_channel）。
        键位不好记，所以右键菜单里也有同名的两项（见 build_feed_menu）。"""
        if not self.feed_sources:
            return False
        if len(self.feed_sources) <= 1:
            self.review_hint("只有一个社区可切", 1200)
            return False
        self._feed_switch_mark("source")
        n = len(self.feed_sources)
        self.feed_source_i = (self.feed_source_i + step) % n
        src = self.feed_source()
        self.config["feed_source"] = getattr(src, "key", "")     # 记 key，别记对象
        self.feed_error = ""
        self.feed_gen += 1
        self.feed_nav.reset()               # 换来源就回列表页：详情是上一家的帖子，留着没意义
        self.feed_nav.channel = self.feed_first_channel()
        self.update_review_bar()
        self.feed_load("list")
        self.feed_scroll_top()
        log_event(f"社区：切到来源 {getattr(src, 'label', '')}")
        return True

    def feed_goto_source(self, index):
        """直接跳到第 index 个来源（右键菜单用）。"""
        if not self.feed_sources:
            return False
        index = max(0, min(len(self.feed_sources) - 1, int(index)))
        if index == self.feed_source_i:
            return False
        return self.feed_cycle_source(index - self.feed_source_i)

    def feed_cycle_channel(self, step):
        """`,` `.` 切栏目（来源内的"热榜/最新"这类，位置相当于 Reddit 的 subreddit）。

        栏目列表完全由来源声明；只有一个栏目时按了没反应——那是正常的，不是坏了。
        换过的栏目按来源记进 config（见 feed_board_key）。"""
        src = self.feed_source()
        if src is None:
            return False
        try:
            channels = list(src.channels() or []) or ["全部"]
        except Exception:
            channels = ["全部"]
        if len(channels) <= 1:
            self.review_hint("这个社区只有一栏（全部）", 1500)
            return False
        self._feed_switch_mark("channel")
        try:
            i = channels.index(self.feed_nav.channel)
        except ValueError:
            # 当前这一栏不在表里（手填的 slug / 记忆里老名字）：就从它接着往后走
            channels = [self.feed_nav.channel] + channels
            i = 0
        self.feed_nav.channel = channels[(i + step) % len(channels)]
        self.config[self.feed_board_key()] = self.feed_nav.channel
        self.feed_gen += 1
        self.feed_nav.reset()
        self.update_review_bar()
        self.feed_load("list")
        self.feed_scroll_top()
        return True

    def feed_goto_channel(self, channel):
        """直接跳到某个栏目/板块（右键菜单、板块选择器用）。"""
        src = self.feed_source()
        nav = self.feed_nav
        if src is None or nav is None:
            return False
        channel = str(channel or "")
        if not channel or channel == nav.channel:
            return False
        self._feed_switch_mark("channel")
        self.feed_nav.channel = channel
        self.config[self.feed_board_key()] = channel
        self.feed_error = ""
        self.feed_gen += 1
        nav.reset()
        # 立刻按新栏目重画一遍（标题+空壳），再去后台取数：以前是先取数、回来了才重画，
        # 中间那几秒屏幕上还是**上一个板块**的内容，人看到的就是"换了板块没刷新"。
        self.render_feed()
        self.update_review_bar()
        self.feed_load("list")
        self.feed_scroll_top()
        log_event(f"社区：切到栏目 {channel}")
        return True

    def feed_load_boards(self):
        """后台拉当前来源的**全量板块表**（只有 NGA 有这么多；别的来源本来就一栏两栏）。

        放在后台是因为它要联网；回来之后弹一个可搜索的列表（见 feed_board_picker）。"""
        src = self.feed_source()
        if src is None or not hasattr(src, "boards") or self.feed_busy:
            return False
        self.feed_busy = True
        self.feed_board_pending = True
        self.feed_boards_tried.add(getattr(src, "key", ""))   # 试过一次就不再自动重试

        def work():
            try:
                self.feed_data_signal.emit("boards", (src.boards(), self.feed_gen,
                                                      getattr(src, "key", "")))
            except Exception as e:
                self.feed_data_signal.emit("error", (str(e), self.feed_gen, getattr(src, "key", "")))

        threading.Thread(target=work, daemon=True).start()
        self.review_hint("正在拉板块列表…", 1200)
        return True

    def feed_board_picker(self):
        """板块选择器：**可搜索**的列表（NGA 有 661 个板块，菜单里放不下）。

        用 Qt 自己的可编辑下拉框：点开就能打字筛，回车选中。选完立刻换栏目并重新取数。"""
        src = self.feed_source()
        if src is None or self.feed_nav is None:
            return False
        if (hasattr(src, "boards") and not getattr(src, "_boards", None)
                and getattr(src, "key", "") not in self.feed_boards_tried):
            # 全量表还没拉过：**绝不能在界面线程里拉**（NGA 那一下要几秒，界面会僵住）。
            # 交给后台，回来再弹选择器（见 feed_load_boards / on_feed_data）。
            return self.feed_load_boards()
        try:
            boards = list(src.boards() or [])
        except Exception:
            boards = []
        if not boards:
            # 没有 boards()（IT之家/知乎/微博/V2EX/linux.do 都是"只有栏目"）：
            # 它的 channels() 就是能切的那几栏，别一口咬定"没有板块"
            try:
                boards = [{"label": ch, "fid": "", "group": ""}
                          for ch in (src.channels() or [])]
            except Exception:
                boards = []
        if not boards:
            self.review_hint("这个来源没有板块列表", 1500)
            return False
        labels = []
        for b in boards:
            group = str(b.get("group") or "").strip()
            heat = str(b.get("heat") or "").strip()
            bits = [x for x in (group, b["label"], heat) if x]
            labels.append(" · ".join(bits))
        cur = 0
        for i, b in enumerate(boards):
            if b["label"] == self.feed_nav.channel:
                cur = i
                break
        title = ("%s · 换板块（共 %d 个，打字筛；也可以直接填 slug，比如 vote）"
                 % (self.feed_source_label(), len(boards)))
        # editable=True：表里搜不到时，可以直接填一个 slug（虎扑那种 /vote、/bxj 的）
        pick, ok = QInputDialog.getItem(self, title, "板块：", labels, cur, True)
        if not ok or not pick:
            return False
        pick = str(pick).strip()
        if pick in labels:
            return self.feed_goto_channel(boards[labels.index(pick)]["label"])
        check = getattr(src, "accepts_channel", None)
        if pick and (not callable(check) or check(pick)):
            return self.feed_goto_channel(pick)
        self.review_hint("这个来源没有「%s」这个板块" % pick[:14], 1800)
        return False

    def feed_source_picker(self):
        """社区（来源）选择器：一个可搜索的列表，选完立刻切过去。"""
        if not self.feed_sources:
            return False
        labels = [getattr(s, "label", "") or getattr(s, "key", "") for s in self.feed_sources]
        cur = max(0, min(len(labels) - 1, self.feed_source_i))
        pick, ok = QInputDialog.getItem(self, "换社区（来源）", "社区：", labels, cur, False)
        if not ok or not pick:
            return False
        return self.feed_goto_source(labels.index(pick))

    def feed_open_original(self):
        """用系统浏览器打开原帖。第一轮**只读**：不发帖、不点赞、不转发（写操作风险最高）。"""
        nav = self.feed_nav
        if nav is None:
            return False
        post = nav.post if nav.view == "detail" else nav.selected()
        url = (post.url if post is not None else "") or ""
        if not url:
            return False
        try:
            os.startfile(url)           # Windows：交给默认浏览器
            log_event(f"社区：打开原帖 {url}")
        except Exception as e:
            log_event(f"社区：打开原帖失败 {e}")
        return False                    # 窗口内容没变，不需要重画

    def on_feed_key(self, key):
        """社区页的窗口内按键（和卡片页同一套机制：只在窗口是活动窗口时响应）。

        翻页键留给滚动——社区流是长文档，滚动比"整屏翻"更合用；书页那套切页逻辑不掺和。"""
        nav = self.feed_nav
        if nav is None:
            return False
        if key in (Qt.Key_J, Qt.Key_Down):
            self.feed_move(1)
            return True
        if key in (Qt.Key_K, Qt.Key_Up):
            self.feed_move(-1)
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.feed_open()
            return True
        if key == Qt.Key_Backspace:
            self.feed_back()
            return True
        if key == Qt.Key_Escape:
            if nav.view == "detail":
                if self._feed_from_answer():
                    # 知乎：回答 → 点标题进问题 这一层，Esc 直接回主页（不先退回那个回答）
                    self.feed_home()
                else:
                    self.feed_back()
                return True
            return False                # 不在详情里就不抢 Esc（老板键可能就挂在它上面）
        if key == Qt.Key_Z:
            # Z：详情里 = 回主页；列表里 = 撤回上一次切社区/切板块
            if nav.view == "detail":
                return self.feed_home()
            return self.feed_undo_switch()
        if key == Qt.Key_E:
            # E：直接到评论区顶部（正文再长也不用一段段滚）
            return self.feed_scroll_comments_top()
        if key == Qt.Key_BracketLeft:
            self.feed_cycle_source(-1)
            return True
        if key == Qt.Key_BracketRight:
            self.feed_cycle_source(1)
            return True
        if key == Qt.Key_Comma:
            self.feed_cycle_channel(-1)
            return True
        if key == Qt.Key_Period:
            self.feed_cycle_channel(1)
            return True
        if key == Qt.Key_R:
            self.feed_reload()
            return True
        if key == Qt.Key_O:
            self.feed_open_original()
            return True
        if key == Qt.Key_T:
            # T：这一场在文字直播和数据统计之间切（只有赛程那种页面才有）
            if self.feed_toggle_view():
                return True
        if key == Qt.Key_C:
            # C = **直接**切到下一个社区（不弹窗、不用确认）；Z 撤回。
            # 想挑某一个再走右键菜单里的"选择社区…"（见 build_feed_menu）
            return self.feed_cycle_source(1)
        if key == Qt.Key_V:
            # V = 直接切到下一个板块（来源内的栏目），Z 撤回
            return self.feed_cycle_channel(1)
        if key in (Qt.Key_Left, Qt.Key_Right):
            # 列多的表：左右箭头左右滑（横条不显示，只有这两个键和滚轮）
            step = self._feed_hstep()
            if self._feed_hslide(-step if key == Qt.Key_Left else step):
                return True
        sb = self.text_edit.verticalScrollBar()
        page = max(1, self.text_edit.viewport().height())
        if key in (Qt.Key_Space, Qt.Key_PageDown):
            sb.setValue(min(sb.maximum(), sb.value() + page))
            return True
        if key == Qt.Key_PageUp:
            sb.setValue(max(sb.minimum(), sb.value() - page))
            return True
        if key == Qt.Key_Home:
            sb.setValue(sb.minimum())
            return True
        if key == Qt.Key_End:
            sb.setValue(sb.maximum())
            return True
        return False

    def toggle_feed_mode(self):
        if self.feed_mode:
            self.exit_feed_mode(reason="hotkey")
        else:
            self.enter_feed_mode()

    def enter_feed_mode(self):
        """切到社区统一流。进之前先把卡片/安全文档收掉（三个内容页互斥），
        书的状态拍快照——退出后原位回书，一个字节的阅读进度都不写。"""
        if self.feed_mode:
            return
        if not FEED_OK or self.feed_nav is None:
            log_event(FEED_IMPORT_HINT or "社区统一流不可用")
            return
        if self.review_mode:
            self.exit_review_mode(reason="feed")
        if self.safe_mode:
            self.exit_safe_mode()
        self.feed_snapshot = self.snapshot_book_state()
        self.dismiss_action_panel(cancelled=True)
        self.feed_mode = True
        self.feed_error = ""
        self.feed_gen += 1
        self.feed_nav.reset()
        self.feed_nav.channel = self.feed_first_channel()   # 栏目落在来源真的有的那一栏上
        self.update_review_bar()
        self.apply_style()              # 先刷样式：别让文字停在别的页面留下的透明态
        self.text_edit.setExtraSelections([])
        self.feed_restore_pos = self.feed_list_pos.get(self._feed_pos_key()) or 0
        self.render_feed()              # 先落一个空壳，正文随后台取数回来
        self.feed_load("list")
        self.update_tray_tooltip()
        log_event("社区统一流：已进入")

    def exit_feed_mode(self, reason=""):
        if not self.feed_mode:
            return
        self._feed_save_pos()           # 下次进来还坐在这儿
        self.feed_mode = False
        if getattr(self, "feed_live_timer", None) is not None:
            self.feed_live_timer.stop()     # 出了社区页就别再问直播了
        snap = self.feed_snapshot
        self.feed_snapshot = None
        self.text_edit.setExtraSelections([])
        if snap:
            # 书页按快照原样回来；**不写阅读进度**（社区页上的坐标不是书的位置）
            self.restore_book_state(snap, write_progress=False)
        # 兜一层：社区页那套字体是**控件级**的（`setFont` 会连文档默认字体一起换），所以退出时
        # 把控件字体和**文档默认字体**都按阅读设置再钉一次。只靠 restore_book_state 里的
        # apply_style 不够稳：退出之后还有几条会重渲染书页的路（本地书重排、变色龙定时器、
        # 设置窗口刷样式、鼠标进出幽灵态）——它们要是拿社区页的字体去铺书页，就是真人反馈的
        # "退出 CAF 之后正文的字体变了"。配置是权威值，这里照它钉，不看上一刻控件里是什么。
        # 2026-10-19：钉的范围扩到**每个字符的 charFormat**（见 `_pin_book_font` 的说明）。
        self._pin_book_font("退出社区页")
        self.update_review_bar()
        self.update_tray_tooltip()
        log_event(f"社区统一流：已退出（{reason or '未说明'}）")

    def anki_client(self):
        """惰性建 AnkiConnect 客户端：不进背诵就完全不碰 Anki。"""
        if self.review_client is None:
            if not ANKI_CLIENT_OK:
                raise AnkiUnavailable(INSTALL_HINT)
            key = (self.config.get("anki_api_key") or "").strip() or None
            self.review_client = AnkiClient(api_key=key)
        return self.review_client

    # --- 后台执行（界面绝不等待 AnkiConnect）---

    def anki_worker_thread(self):
        if self.anki_worker is None:
            self.anki_worker = AnkiWorker(self)
            self.anki_worker.done.connect(self.on_anki_done)
            self.anki_worker.failed.connect(self.on_anki_failed)
            self.anki_worker.start()
        return self.anki_worker

    def anki_call(self, fn, on_ok=None, on_err=None, key=None):
        """把一次 AnkiConnect 调用丢给后台线程；同一个 key 没跑完就不再排第二个。

        去重很要紧：Anki 一忙（同步中）对账会一次比一次慢，不去重就会堆一队 60ms 的调用，
        界面反而更卡。评分用 key='grade'，所以连按两下也不会答两张卡。
        """
        if key and key in self.anki_inflight:
            return False
        self.anki_task_seq += 1
        task_id = self.anki_task_seq
        if key:
            self.anki_inflight.add(key)
        self.anki_tasks[task_id] = {"ok": on_ok, "err": on_err, "key": key}
        self.anki_worker_thread().submit(task_id, fn)
        return True

    def _finish_anki_task(self, task_id):
        task = self.anki_tasks.pop(task_id, None)
        if task and task.get("key"):
            self.anki_inflight.discard(task["key"])
        return task

    def on_anki_done(self, task_id, result):
        task = self._finish_anki_task(task_id)
        if not task or not task.get("ok"):
            return
        try:
            task["ok"](result)
        except Exception as e:
            log_event(f"Anki 回调异常：{type(e).__name__}")

    def on_anki_failed(self, task_id, exc):
        task = self._finish_anki_task(task_id)
        if not task or not task.get("err"):
            log_event(f"Anki 调用失败：{type(exc).__name__}")
            return
        try:
            task["err"](exc)
        except Exception as e:
            log_event(f"Anki 错误回调异常：{type(e).__name__}")

    def stop_anki_worker(self):
        if self.anki_worker is not None:
            try:
                self.anki_worker.stop()
                self.anki_worker.wait(1500)
            except Exception:
                pass
            self.anki_worker = None

    def shutdown_anki(self):
        """退出程序前的收尾：把 Anki 从"复习器 + 藏起来的窗口"里放出来，再停后台线程。

        顺序要紧：`exit_review_mode` 里的离开/还原是异步排队的，紧接着就停线程会把那个任务
        丢掉，Anki 窗口就会一直留在藏起来的状态（任务栏里找不着、Alt+Tab 也没有）。
        这里同步兜一次，然后才停线程——否则 QThread 还在跑就被析构，PyQt 会直接崩。
        """
        if self.review_client is not None:
            try:
                self.review_client.leave_review()
            except Exception:
                pass
            try:
                self.review_client.reveal_anki()   # 还原也不激活：绝不把 Anki 亮到最前面
                self.forget_anki_concealed()
            except Exception:
                pass
        self.stop_anki_worker()

    def forget_anki_concealed(self):
        """清掉"我们把 Anki 窗口藏起来了"的落盘标记（确认已经放出来时调）。"""
        if self.config.pop("anki_concealed_by_us", None) is not None:
            try:
                self.save_config()
            except Exception:
                pass

    def recover_anki_window(self):
        """启动时的窗口体检：上次进程要是被强杀的，Anki 可能还停在我们藏起来的状态。

        有落盘标记就**显示**它；没标记只做位置体检（整个落在屏幕外才搬回来），
        免得把人自己最小化的 Anki 无端弹出来。
        """
        try:
            client = self.anki_client()
            if self.config.get("anki_concealed_by_us"):
                log_event("上次没来得及还原 Anki 窗口，启动补一次")
                client.ensure_anki_visible()
                self.forget_anki_concealed()
            else:
                hwnd = client.anki_hwnd()
                if hwnd and client.ensure_on_screen(hwnd):
                    log_event("Anki 窗口在屏幕外，已搬回主屏")
        except Exception as e:
            log_event(f"Anki 窗口体检失败: {type(e).__name__}")

    def rescue_anki_window(self):
        """托盘手动兜底：把 Anki 窗口从"被藏着 / 最小化 / 屏幕外"救回来。"""
        try:
            client = self.anki_client()
            client.ensure_anki_visible(activate=True)
            self.forget_anki_concealed()
            log_event("手动叫回 Anki 窗口")
        except Exception as e:
            log_event(f"叫回 Anki 窗口失败: {type(e).__name__}: {e}")


    def review_status_text(self):
        """状态文案：牌组 · 待复习 N（Anki 每日上限口径） · 今日已复习 M · 本次 K。"""
        s = self.review_session or {}
        parts = [str(s.get("deck", "?"))]
        if s.get("remaining") is not None:
            parts.append(f"待复习 {s['remaining']}")
        if s.get("reviewed_today") is not None:
            parts.append(f"今日已复习 {s['reviewed_today']}")
        if s.get("counted"):
            parts.append(f"本次 {s['counted']}")
        return " · ".join(parts)

    def review_hint(self, text, ms=1800):
        """提示只走浮动气泡：绝不往正文里写一个字（写正文会毁掉翻页测量，也会盖掉卡片）。"""
        try:
            QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, 8)), text, self,
                              QRect(0, 0, 1, 1), ms)
        except Exception:
            pass

    @staticmethod
    def review_err_text(e):
        """错误文案只留动作名与异常类型——**卡片正文/答案/音频名绝不进日志与提示**。"""
        return type(e).__name__

    def enter_review_mode(self, deck=None):
        """进隐形背诵。真正的 Anki 往返全丢给后台线程——实测那一串（连 Anki + 开牌组 +
        取卡 + 取计数 + 取媒体目录）要 0.7 秒，放主线程就是"按下去先僵一下"。"""
        if self.review_mode or self.review_entering:
            return True
        deck = (deck or self.config.get("anki_deck") or "").strip()
        if not deck:
            # 第一次用还没选牌组：把设置打开（那里有牌组列表，能直接读 Anki 的实际牌组）
            self.review_hint("还没选牌组：在设置里挑一个（可以点「列出牌组…」）", 4000)
            QTimer.singleShot(0, self.open_settings)
            return False
        try:
            client = self.anki_client()        # 只建对象，没有网络往返，秒回
        except AnkiError as e:
            self.review_hint(INSTALL_HINT, 6000)
            log_event(f"进背诵失败：{self.review_err_text(e)}")
            return False
        # 偷瞄中按背诵键（或从托盘进）：**保持偷瞄**，只把内容换成卡片——可见层与内容层正交。
        # 钩子继续装着，从卡片铺好的那一刻起它会全局捕获卡片交互键（可以不聚焦窗口地背）。
        if self.safe_mode:
            self.exit_safe_mode()              # 从安全文档进来：先把真书放回来，再拍快照
        if self.feed_mode:
            self.exit_feed_mode(reason="review")   # 从社区页进来：同上，内容页互斥
        self.review_entering = True
        self.review_hint(f"正在打开牌组「{deck}」…", 1500)

        def job():
            client.ping()
            client.start_review(deck)
            return {"card": client.current_card(),
                    "stats": client.deck_stats([deck]),
                    "reviewed": client.reviewed_today(),
                    "media": client.media_dir() or ""}

        self.anki_call(job, on_ok=lambda r, d=deck: self.finish_enter_review(d, r),
                       on_err=self.fail_enter_review, key="enter")
        return True

    def fail_enter_review(self, exc):
        self.review_entering = False
        if isinstance(exc, AnkiUnavailable):
            self.review_hint(INSTALL_HINT, 6000)
        elif isinstance(exc, AnkiBusy):
            self.review_hint("Anki 正在同步/忙，稍等一下再按一次", 4000)
        else:
            self.review_hint(f"进背诵失败：{self.review_err_text(exc)}", 4000)
        log_event(f"进背诵失败：{self.review_err_text(exc)}")

    def finish_enter_review(self, deck, r):
        """后台已经把牌组打开好了：切内容、装钩子、铺第一张卡（这一段没有任何网络往返）。"""
        self.review_entering = False
        if self.review_mode:
            return
        self.review_media_dir = r.get("media") or ""
        self.review_poll_tick = 0
        self.dismiss_action_panel(cancelled=True)
        self.review_snapshot = self.snapshot_book_state()
        self.review_session = {"deck": deck, "card": None, "card_id": None, "shown": False,
                               "counted": 0, "remaining": None, "reviewed_today": None,
                               "sounds": [], "busy_retries": 0}
        self.review_mode = True
        # 借用本地渲染管线：字体/字号/行距/页边距/缩进全部沿用，卡片看起来就是这份文档。
        # local_full_text 故意留空 —— "进度跟着视线走"、翻页、划线会因此自动闭嘴
        # （displayed_is_local_page 返回 False），不必到处打补丁。
        self.is_local_mode = True
        self.local_file_path = ""
        self.local_full_text = ""
        self.local_start_index = 0
        self.local_page_history = []
        self.local_chapters = []
        self.local_heading_offsets = set()
        self.displaying_local_text = True
        self.ghost_veiled = False
        self.apply_style()
        if not self.isVisible():
            self.show()                    # WA_ShowWithoutActivating：不抢工作窗口的焦点
        self.raise_()
        self.ensure_on_screen()
        self.review_timer.start()          # 卡片交互键全在窗口内（keyPressEvent），不装任何钩子
        if self.config.get("anki_conceal", True):
            try:
                if self.anki_client().conceal_anki():   # 纯 win32 调用，不排 Anki 的队
                    # 落盘标记：这个进程要是被任务管理器/崩溃干掉，下次启动还知道要把它放出来
                    self.config["anki_concealed_by_us"] = True
                    self.save_config()
            except Exception:
                pass
        self.apply_counts(r.get("stats"), r.get("reviewed"))
        self.review_load_card(r.get("card"))
        log_event(f"隐形背诵：已进入（牌组 {deck}）")

    def apply_review_bar_style(self):
        """页脚的字号/颜色跟着阅读设置走：字号贴近正文（约 1.15 倍换算，不再是小一号的 11px），
        不透明度 0.92。

        **整条页脚不设底色、也没有按钮**（真人反馈："这个区块不要用背景色，太显眼"、
        "不用按钮，有有效信息就行"）——它就是正文下面两行淡字，压在那里不该看出来是个面板。
        """
        size = self.review_bar_font_px()
        color = QColor(200, 200, 200)
        try:
            base = QColor(str(self.config.get("text_color") or ""))
            if base.isValid():
                color = base
        except Exception:
            pass
        r, g, b = color.red(), color.green(), color.blue()
        c = f"rgba({r},{g},{b},235)"
        self.review_bar.setStyleSheet(
            f"QWidget#reviewBar {{ background: transparent; }}"
            f"QLabel {{ color: {c}; font-size: {size}px; background: transparent; }}")

    def update_review_bar(self):
        """页脚内容：还剩多少（新/学/复 + 今日已/本次）+ 四个评分键各自的后果。

        后果那行来自 Anki 的 `nextReviews`（"重来 <10 分 / 良好 3 天"），也就是**按下去
        会对这张卡产生什么**——这是整块页脚存在的理由。学习步/FSRS 下按钮可能只有 2 个。
        """
        if not hasattr(self, "review_bar"):
            return
        if self.feed_mode:
            # 社区页复用同一条页脚：给的是状态（第几条 / 评论多少 / 还有没有下一页），
            # 照样没有按钮、没有键位提示——真人给页脚定的两条规矩原样沿用。
            visible = True
        else:
            visible = bool(self.review_mode and not self.safe_mode)
        if not self.config.get("show_bar", True):
            visible = False          # 设置里关掉了底部信息栏：页面只剩正文
        self.review_bar.setVisible(visible)
        if not visible:
            return
        self.apply_review_bar_style()
        self.fill_review_bar()

    def review_bar_font_px(self):
        """页脚实际用的字号（px）——`apply_review_bar_style` 的那条换算，只此一处定义。"""
        return max(13, int(round(int(self.config.get("font_size", 14)) * 1.15)))

    def _review_bar_metrics(self):
        """按**页脚真实字号**量文字宽度。

        不能用 `review_bar.fontMetrics()`：那是页脚控件自己的默认字体（约 12px），
        而按钮上的字是样式表给的 16px——量出来偏小，判"够宽"于是永远不触发紧凑模式（踩过）。
        """
        font = QFont(self.config.get("font_family", "Microsoft YaHei"))
        font.setPixelSize(self.review_bar_font_px())
        return QFontMetrics(font)

    def fill_review_bar(self):
        """页脚的文字：学习计划 + 每个评分键按下去会变成什么。**没有按钮、没有操作提示。**

        真人两条原话定死了这块长什么样：
          - "重来困难下一次就多的信息消失了，这个很重要" → 间隔信息一个字都不能省；
          - "不用按钮，有有效信息就行" + "不用操作提示，只要必要的信息" → 不做按钮、不写提示。

        折行自己算（不交给 QLabel 的 wordWrap：那会在"1 重来"和"<1 分"中间断开）：
        每行按页脚真实字号量宽度，放不下就换行，所以页脚高度严格等于行数——没有空行、
        没有固定高度。窗口宽度变了只要重跑这里（`repage_after_resize` 会调）。
        """
        if not hasattr(self, "review_bar") or not self.review_bar.isVisible():
            return
        if self.feed_mode:
            # 社区页那一行：只有状态，没有按钮/提示（内容由 feed.status_text 统一给）
            self.review_info_label.setText(self.feed_status_text())
            return
        s = self.review_session or {}
        card = s.get("card") or {}
        buttons = list(card.get("buttons") or [1, 2, 3, 4])
        times = list(card.get("nextReviews") or [])
        labels = REVIEW_BUTTON_LABELS.get(len(buttons))
        if labels is None:
            labels = ["重来", "困难", "良好", "容易"][:len(buttons)]
        fm = self._review_bar_metrics()
        avail = max(120, self.width() - 20)
        # 一、还剩多少（Anki 的每日上限口径）
        head = ""
        if s.get("new_count") is not None:
            head = f"新 {s.get('new_count')} · 学 {s.get('learn_count')} · 复 {s.get('review_count')}"
            if s.get("reviewed_today") is not None:
                head += f"　今日已 {s['reviewed_today']}"
            if s.get("counted"):
                head += f" · 本次 {s['counted']}"
        # 二、四个评分键各自的后果（Anki 的 nextReviews，U+2066/U+2069 已剔掉）
        entries = []
        for i in range(len(buttons)):
            name = labels[i] if i < len(labels) else str(buttons[i])
            when = REVIEW_TEXT_JUNK.sub("", str(times[i])) if i < len(times) else ""
            entries.append(f"{buttons[i]} {name}" + (f" {when}" if when else ""))
        lines = []
        items = ([head] if head else []) + entries
        cur = ""
        for item in items:
            cand = f"{cur}　{item}" if cur else item
            if cur and fm.horizontalAdvance(cand) > avail:
                lines.append(cur)
                cur = item
            else:
                cur = cand
        if cur:
            lines.append(cur)
        self.review_info_label.setText("\n".join(lines))

    def apply_counts(self, stats, reviewed):
        """把 Anki 给的计数装进会话（数据都由后台线程取来，这里一次网都不连）。

        计数一律用 Anki 口径：`getDeckStats` 已经把每日上限算进去了，
        `getNumCardsReviewedToday` 是 Anki 自己记的"今日已复习"。
        """
        s = self.review_session
        if not s:
            return
        if isinstance(stats, dict):
            info = stats.get(s.get("deck")) or {}
            if info:
                s["new_count"] = int(info.get("new", 0))
                s["learn_count"] = int(info.get("learn", 0))
                s["review_count"] = int(info.get("review", 0))
                s["remaining"] = s["new_count"] + s["learn_count"] + s["review_count"]
        if reviewed is not None:
            s["reviewed_today"] = int(reviewed)
        self.update_review_bar()
        self.update_tray_tooltip()

    def review_load_card(self, card):
        """换一张卡：只渲染**正面**；计时交给后台线程（一个 60ms 的调用不该卡住换卡）。"""
        s = self.review_session
        if not s:
            return
        if not card:
            s["card"] = None
            s["card_id"] = None
            s["shown"] = False
            self.review_render_placeholder("这个牌组今天已经清完了")
            self.update_review_bar()
            self.update_tray_tooltip()
            return
        s["card"] = card
        s["card_id"] = card.get("cardId")
        s["shown"] = False                 # 翻面状态自己记：Anki 的 answer 翻面前就渲染好了
        self.render_card()
        client = self.anki_client() if ANKI_CLIENT_OK else None
        if client is not None:
            self.anki_call(lambda: client.start_card_timer(), key="timer")   # 让它自己回去，不等
        self.update_review_bar()
        self.update_tray_tooltip()

    def review_render_placeholder(self, text):
        """没卡/出错时的占位：也要像一份文档的正文，不能给一张空白页。"""
        s = self.review_session
        if s is not None:
            s["seq"] = int(s.get("seq", 0)) + 1
        self.text_edit.document().setDefaultStyleSheet("")
        self.text_edit.setHtml(f'<div style="color:#999999;">{text}</div>')
        self.apply_card_style()

    def render_card(self, show_answer=None):
        """把当前这张卡渲染进正文区。

        **只渲染该显示的那一面**：实测 `guiCurrentCard` 的 `answer` 在没翻面时就已经渲染好了，
        拿它当"翻面了没"会直接把答案亮在正面——伪装当场报废。
        """
        if self.safe_mode:
            return      # 安全文档正盖在上面：不许重渲卡片，否则那张假周报会被卡片顶掉
        s = self.review_session or {}
        card = s.get("card")
        if not card:
            return
        # 屏幕版本号：每真正换一次内容就 +1。后台调用（对账/翻面/撤销）拿它当校验——
        # 发出去之后屏幕若已经换过卡，那趟结果就是**旧快照**，照它渲染会把新卡盖回去。
        s["seq"] = int(s.get("seq", 0)) + 1
        show = bool(s.get("shown", False)) if show_answer is None else bool(show_answer)
        raw = card.get("answer") if show else card.get("question")
        html, sounds = simplify_anki_html(raw)
        s["sounds"] = sounds
        doc = self.text_edit.document()
        doc.setDefaultStyleSheet(filter_anki_css(card.get("css")))
        media = self.review_media_dir or ""      # 进牌组时缓存好的，换卡不再连 Anki
        base = QUrl.fromLocalFile(str(media) + os.sep) if media else None
        if base is not None:
            doc.setBaseUrl(base)
        self.text_edit.setHtml(html or "(这张卡没有内容)")
        if base is not None:
            doc.setBaseUrl(base)           # setHtml 之后再钉一次，图片按 collection.media 解析
        self.apply_card_style()
        self.update_review_bar()           # 翻面后页脚按钮要跟着启用（翻面前是灰的）

    def apply_card_style(self):
        """卡片自己那套排版：居中、不首行缩进、不两端对齐、正常行距，字号沿用阅读字号。

        书页那套（首行缩进两字 + 两端对齐 + 行距 1.6）放在卡片上会让它一眼不像文档模板，
        所以按卡片重套一遍块格式；颜色/透明度仍由 apply_style 那套统管。
        """
        self.text_edit.setFont(QFont(self.config.get("font_family", "Microsoft YaHei"),
                                     self.config["font_size"]))
        block_format = QTextBlockFormat()
        block_format.setLineHeight(100, QTextBlockFormat.ProportionalHeight)
        block_format.setAlignment(Qt.AlignHCenter)
        block_format.setTextIndent(0)
        block_format.setTopMargin(0)
        block_format.setBottomMargin(0)
        cursor = QTextCursor(self.text_edit.document())
        cursor.select(QTextCursor.Document)
        cursor.mergeBlockFormat(block_format)
        self.apply_page_margins(cut=False)   # 卡片可以滚动，不做书页那种"退到整行"的底部裁切
        self.text_edit.verticalScrollBar().setValue(0)

    def toggle_review_mode(self):
        if self.review_mode:
            self.exit_review_mode(reason="hotkey")
        else:
            self.enter_review_mode()

    def exit_review_mode(self, restore=True, reason=""):
        """退出背诵：书页按快照原样回来，而且**不写阅读进度**（卡片坐标不是书的位置）。"""
        if not self.review_mode:
            return
        session = self.review_session or {}
        snap = self.review_snapshot
        self.review_mode = False
        self.review_entering = False
        self.review_session = None
        self.review_snapshot = None
        self.update_review_bar()   # 页脚信息立刻收掉，别留在书页上
        if self.review_client is not None:
            client = self.review_client
            if self.anki_worker is not None:
                # 也走后台：退出的那 60ms 不该卡住界面。退出程序时 quit_app 会再同步兜一次，
                # 保证 Anki 窗口绝对不会留在"藏起来"的状态。
                self.anki_call(lambda: (client.leave_review(), client.reveal_anki()),
                               on_ok=lambda _r: self.forget_anki_concealed(), key="leave")
        if restore and snap:
            # 安全文档正盖在卡片上（而且就是从背诵切过来的）：一起收掉，直接回到书。
            # 否则会走进"从安全文档退出→回到卡片"的分支，而背诵这时已经退出了，留下一屏卡片残影。
            if self.safe_mode and (self.safe_snapshot or {}).get("from_review"):
                self.safe_mode = False
                self.safe_snapshot = None
            self.restore_book_state(snap, write_progress=False)
        self.review_timer.stop()
        self.update_tray_tooltip()
        log_event(f"隐形背诵：已退出（{reason or '未说明'}，本次背了 {session.get('counted', 0)} 张）")

    # --- 复习键 ---

    def review_swallow_all(self):
        """偷瞄时是否连空格/回车也全局捕获（设置项；关掉则只有 1-4/U/R/Esc 全局，
        空格/回车留给底下的应用打字）。窗口内不影响：窗口内空格/回车一直是翻面键。"""
        return bool(self.config.get("anki_swallow_keys", True))

    def peek_captures_review_keys(self):
        """偷瞄这一层（全局层）当前该不该捕获卡片交互键。

        只有"偷瞄中 + 当前页是卡片 + 没被安全文档盖住"才捕获。窗口内（非偷瞄）一个钩子
        都不装：卡片交互键和书页一样走窗口 `keyPressEvent`，只有窗口是当前活动窗口时
        才响应——在别的软件里打字再也不会把卡片翻掉或评掉。
        """
        return bool(self.peek_mode and self.review_mode and not self.safe_mode)

    def peek_captures_feed_keys(self):
        """偷瞄这一层要不要把社区键收下来：偷瞄中 + 当前页是社区 + 没被安全文档盖住。"""
        return bool(self.peek_mode and self.feed_mode and not self.safe_mode)

    def on_feed_peek_key(self, key):
        """偷瞄时收到的社区键（信号从钩子线程来，这里是主线程）。"""
        if not self.feed_mode:
            return
        self.on_feed_key(int(key))

    def peek_captures_book_keys(self):
        """偷瞄这一层要不要把书页的翻页/滚动键收下来。

        社区页和卡片页各有自己的一套（见上面两个捕获），这里只管书页。
        安全文档也算书页：内容不同，翻页方式一样。"""
        if not self.peek_mode:
            return False
        if self.feed_mode:
            return False
        if self.review_mode and not self.safe_mode:
            return False
        return True

    def on_peek_book_key(self, name):
        """偷瞄时收到的书页键（主线程）：上下箭头逐行滚，其余整页翻。"""
        if not self.peek_mode:
            return
        if name == "down":
            self.scroll_lines(1)
        elif name == "up":
            self.scroll_lines(-1)
        elif name in ("space", "pagedown", "right"):
            self.scroll_page(1)
        elif name in ("pageup", "left"):
            self.scroll_page(-1)

    def on_review_key(self, name):
        """复习键回调。信号从钩子线程发过来，QueuedConnection 保证在这里是主线程。"""
        if not self.review_mode:
            return
        s = self.review_session or {}
        try:
            if name in ("space", "enter"):
                if s.get("shown"):
                    # 翻面之后空格**不评分**：Anki 那边空格=良好，误按一下就把这张卡的
                    # 排程改了；要评分就明确按 1-4（Anki 给的按钮顺序）
                    self.review_hint("用 1/2/3/4 评分（按 Anki 给的按钮顺序）")
                else:
                    self.review_show_answer()
            elif name in ("1", "2", "3", "4"):
                self.review_grade_button(int(name))
            elif name == "u":
                self.review_undo()
            elif name == "r":
                self.review_play_audio()
            elif name == "esc":
                self.exit_review_mode(reason="esc")
        except Exception as e:
            log_event(f"复习键处理异常：{type(e).__name__}")   # 只记类型，不记卡片内容

    def review_show_answer(self):
        """翻面：让 Anki 翻（后台线程），拿到回执再渲染答案那面并放这一面的音频。"""
        s = self.review_session
        if not s or not s.get("card") or s.get("shown"):
            return
        if "reveal" in self.anki_inflight:
            return
        client = self.anki_client()
        prev_id = s.get("card_id")
        at_seq = int(s.get("seq", 0))

        def job():
            client.show_answer()
            client.play_audio()          # 翻面自动放这一面（Anki 自己的语义）
            return client.current_card()

        def done(card):
            s2 = self.review_session
            if not s2 or s2.get("card_id") != prev_id or int(s2.get("seq", 0)) != at_seq:
                return                   # 这中间已经翻页/退出了：结果作废，别盖回来
            s2["shown"] = True
            if card:
                s2["card"] = card
            self.render_card()

        def failed(exc):
            if isinstance(exc, AnkiBusy):
                self.review_note_busy()
            else:
                self.review_hint(f"翻面失败：{self.review_err_text(exc)}", 2500)
                self.review_poll(force=True)

        self.anki_call(job, on_ok=done, on_err=failed, key="reveal")

    def review_play_audio(self):
        """放当前这一面的音频。交给 Anki 自己的 guiPlayAudio：它懂 [sound:]、{{tts:}} 与
        自动播放策略，比我们自己抽标签再找播放器准得多。"""
        if not self.review_mode or not ANKI_CLIENT_OK:
            return
        client = self.anki_client()
        self.anki_call(lambda: client.play_audio(), key="audio")

    def review_grade_button(self, button):
        """按 Anki 实际给出的按钮评分（全程在后台线程，界面不等）。

        `buttons` 是唯一真源（通常 [1,2,3,4]，学习步/FSRS 下可能只有 2 个）；按到没提供的
        那个 Anki 会**返回 False**，这时当"没评上"处理，绝不假装前进。
        对账也在后台做：屏幕上的 cardId 和 Anki 的当前卡不一致就只重渲、这一下不评分。
        """
        s = self.review_session
        if not s or not s.get("card"):
            return
        if not s.get("shown"):
            # 和 Anki 一样：没翻面就没有评分按钮。键盘是全局可达的（偷瞄层），
            # 少了这道门，翻面前手滑按到 1-4 就会把卡片直接评掉。
            self.review_hint("先翻面再评分（空格 / 点一下卡片）", 2000)
            return
        buttons = s["card"].get("buttons") or [1, 2, 3, 4]
        if button not in buttons:
            self.review_hint(f"这张卡只有这些按钮：{'/'.join(str(b) for b in buttons)}", 2600)
            return
        if "grade" in self.anki_inflight:
            self.review_hint("上一张还在处理…", 1200)   # 连按两下也绝不会答两张卡
            return
        prev_id = s.get("card_id")
        deck = s.get("deck")
        client = self.anki_client()

        def job():
            cur = client.current_card()
            if (cur or {}).get("cardId") != prev_id:
                return {"stale": True, "card": cur}
            accepted, nxt = client.answer_and_next(button, prev_id)
            if not accepted:
                return {"accepted": False, "card": cur}
            return {"accepted": True, "card": nxt,
                    "stats": client.deck_stats([deck]), "reviewed": client.reviewed_today()}

        self.anki_call(job, on_ok=lambda r: self.finish_grade(r),
                       on_err=lambda e, b=button: self.fail_grade(b, e), key="grade")

    def finish_grade(self, r):
        s = self.review_session
        if not s:
            return
        if r.get("stale"):
            self.review_load_card(r.get("card"))
            self.review_hint("这张卡已经变了，已换成 Anki 当前的卡", 2600)
            return
        if not r.get("accepted"):
            self.review_load_card(r.get("card"))
            self.review_hint("Anki 没接受这个评分（按钮变了？已重新对账）", 3000)
            return
        s["counted"] = int(s.get("counted", 0)) + 1
        s["busy_retries"] = 0
        self.apply_counts(r.get("stats"), r.get("reviewed"))
        self.review_load_card(r.get("card"))
        log_event(f"隐形背诵：评分成功（本次第 {s['counted']} 张）")

    def fail_grade(self, button, exc):
        if isinstance(exc, AnkiBusy):
            self.review_note_busy(pending=button)
        else:
            self.review_hint(f"评分失败：{self.review_err_text(exc)}", 3000)
            self.review_poll(force=True)

    def review_note_busy(self, pending=None):
        """Anki 正忙（同步 / 媒体检查 / 导入 / 查库）——**绝不把评分悄悄吞掉**。

        退避重试（2 秒起，最多 5 次），并把状态讲清楚；实在评不上就明确提示，
        让人自己决定是等一下还是回 Anki 里点。
        """
        s = self.review_session
        if s is None:
            return
        retries = int(s.get("busy_retries", 0))
        wait = 2.0
        self.review_busy_until = time.time() + wait
        if pending and retries < 5:
            s["busy_retries"] = retries + 1
            self.review_hint(f"Anki 正在同步/忙，{int(wait)} 秒后自动重试这次评分", 3000)
            QTimer.singleShot(int(wait * 1000) + 200, lambda b=pending: self.review_grade_button(b))
        else:
            self.review_hint("Anki 一直在忙，这次评分没记上——等它同步完再按一次", 4000)
        log_event("隐形背诵：Anki 忙（已退避重试）")

    def review_poll(self, force=False, with_counts=False):
        """取一次 Anki 的当前卡，和屏幕上的对账（后台线程，界面不等）。

        计数那两趟（getDeckStats 也是 60ms）不必每拍都拉，默认每 4 拍一次。
        """
        s = self.review_session
        if not s or not ANKI_CLIENT_OK:
            return
        if "poll" in self.anki_inflight:
            return
        deck = s.get("deck")
        client = self.anki_client()
        self.review_poll_tick += 1
        want_counts = bool(with_counts or force or self.review_poll_tick % 4 == 0)
        at_seq = int(s.get("seq", 0))     # 发车时屏幕是第几版

        def job():
            out = {"card": client.current_card()}
            if want_counts:
                out["stats"] = client.deck_stats([deck])
                out["reviewed"] = client.reviewed_today()
            return out

        self.anki_call(job, on_ok=lambda r, f=force, q=at_seq: self.finish_poll(r, f, q),
                       on_err=self.fail_poll, key="poll")

    def finish_poll(self, r, force=False, at_seq=None):
        s = self.review_session
        if not s:
            return
        if at_seq is not None and int(s.get("seq", 0)) != at_seq:
            # 这趟发出去之后屏幕已经换过卡（评分/翻面/撤销回来了）。它读到的是换卡**之前**
            # 的快照：照着它加载会把刚评完的新卡又盖回上一张——看着就是"翻页了但内容没变"。
            return
        if "stats" in r:
            self.apply_counts(r.get("stats"), r.get("reviewed"))
        card = r.get("card")
        if not card:
            if s.get("card") is not None:
                s["card"] = None
                s["card_id"] = None
                s["shown"] = False
                self.review_render_placeholder("这个牌组今天已经清完了")
                self.update_review_bar()
            return
        if s.get("card_id") != card.get("cardId"):
            self.review_load_card(card)
            if force:
                self.review_hint("这张卡已经变了，已换成 Anki 当前的卡", 2600)

    def fail_poll(self, exc):
        if isinstance(exc, AnkiBusy):
            self.review_busy_until = time.time() + 3.0    # 它在忙就别催，安静等
            return
        log_event(f"对账失败：{self.review_err_text(exc)}")

    def review_undo(self):
        """撤销。注意 Anki 这个是**全局**撤销栈（collection.undo），不只是撤销上一次评分。"""
        if not self.review_mode:
            return
        client = self.anki_client()
        deck = (self.review_session or {}).get("deck")
        at_seq = int((self.review_session or {}).get("seq", 0))

        def job():
            client.undo()
            return (client.current_card(), client.deck_stats([deck]) if deck else None)

        def done(res):
            card, stats = res
            s = self.review_session
            if s is not None and int(s.get("seq", 0)) != at_seq:
                return              # 期间已经换卡：撤销的结果不许盖回去
            if s and s.get("counted"):
                s["counted"] = int(s["counted"]) - 1
            self.apply_counts(stats, None)
            self.review_load_card(card)
            self.review_hint("已撤销（Anki 的全局撤销栈）", 2000)

        def failed(exc):
            if isinstance(exc, AnkiBusy):
                self.review_note_busy()
            else:
                self.review_hint(f"撤销失败：{self.review_err_text(exc)}", 2500)

        self.anki_call(job, on_ok=done, on_err=failed, key="undo")

    def review_tick(self):
        """低频对账的节拍：窗口可见时才查（实际查询在后台线程）。"""
        if not self.review_mode or not self.isVisible() or self.isMinimized():
            return
        if time.time() < self.review_busy_until:
            return
        self.review_poll()

    def adjust_color_to_background(self):
        if not self.isVisible() or not self.config.get("auto_mode"):
            self.chameleon_timer.stop()
            return

        screen = QApplication.primaryScreen()
        if not screen: return

        pick_x = self.x() - 5
        pick_y = self.y() + 10

        if pick_x < 0:
            pick_x = self.x() + self.width() + 5

        pixmap = screen.grabWindow(0, pick_x, pick_y, 1, 1)
        img = pixmap.toImage()

        if img.width() > 0:
            color = img.pixelColor(0, 0)
            brightness = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()

            self.content_frame.set_auto_bg_color(color)
            base_text_color = (0, 0, 0) if brightness > 128 else (255, 255, 255)
            user_alpha = int(self.config.get("opacity", 0.9) * 255)
            rgba_color = f"rgba({base_text_color[0]}, {base_text_color[1]}, {base_text_color[2]}, {user_alpha})"

            self.text_edit.setStyleSheet(f"""
                QTextEdit {{
                    color: {rgba_color};
                    background-color: transparent;
                    padding: 0px; margin: 0px; border: none;
                }}
            """)

    def apply_style(self):
        # 刷样式会重排（字号/行距/页边距都在里面），重排前先认住"现在正看的那一行"，
        # 重绘时拿它当页首——否则每次刷样式都从本页最上面重铺，看着就是"阅读进度老是跳回"。
        anchor = self.current_top_offset()
        font_family = self.config.get('font_family', 'Microsoft YaHei')
        font_size = self.config['font_size']
        font = QFont(font_family, font_size)

        self.text_edit.setFont(font)
        # 文档默认字体跟着一起钉：社区页/卡片页会把它换掉，而书页重排时"没写内联字体的那些块"
        # 吃的就是它——只改控件字体不改它，退出社区页后仍可能露出一块社区页的字号。
        self.text_edit.document().setDefaultFont(QFont(font))

        fm = QFontMetrics(font)
        # 最小窗口高度按实际排版算（行距 + 上下页边距），否则压到一行高时会被裁掉
        self.single_line_height = max(
            1,
            int(fm.lineSpacing() * self.line_spacing_percent() / 100) + 2 * self.page_margin_px(),
        )
        self.apply_text_format()
        base_css = "padding: 0px; margin: 0px; border: none;"

        if self.config.get("auto_mode", False):
            self.setWindowOpacity(1.0)
            self.content_frame.set_mode(True)
            self.content_frame.setStyleSheet("background: transparent; border: none;")
            self.content_frame.set_draw_corners(bool(self.config.get("show_corners", True)))
            self.chameleon_timer.start()
            self.adjust_color_to_background()
        else:
            self.chameleon_timer.stop()
            self.content_frame.set_draw_corners(False)
            self.setWindowOpacity(self.config["opacity"])
            self.setStyleSheet("")
            self.content_frame.set_mode(False)
            frame_style = f"""
                CornerFrame {{
                    background-color: {self.config['bg_color']};
                    border-radius: 5px;
                }}
            """
            self.content_frame.setStyleSheet(frame_style)
            text_style = f"""
                QTextEdit {{
                    color: {self.config['text_color']};
                    background-color: transparent;
                    {base_css}
                }}
            """
            self.text_edit.setStyleSheet(text_style)

        # 本地模式下样式改了就得重排重铺（页边距/字号/行距都在这条路上）。
        # 这句原来只写在"非自动挡"分支里，自动挡（变色龙）下去改页边距/字号就不重排，
        # 得等下一次翻页才生效——看着就是"调了没反应"。按锚点重绘，落点不变。
        if self.feed_mode:
            self.render_feed()     # 社区页跟着字号/字体/颜色一起变（和书页、卡片页同一套设置）
        elif self.review_mode and not self.safe_mode:
            self.render_card()     # 背诵中改字号/字体：只重渲当前卡（一个字节的进度都不碰）
        elif self.is_local_mode:
            self.repage_to(anchor)
        self.update_review_bar()   # 页脚（还剩多少 + 各评分键的后果）跟着字号/颜色与模式走

    def enterEvent(self, event):
        self.is_mouse_in = True
        self.ghost_veiled = False  # 回来就把划线一起显示出来
        if self.config.get("ghost_mode", False):
            self.apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_mouse_in = False
        if self.peek_mode:
            return  # 偷瞄期间光标在外面是常态，别把窗口淡到透明
        if self.is_settings_open or self.is_popup_open or self.is_resizing or self.is_moving:
            return

        global_pos = QCursor.pos()
        local_pos = self.mapFromGlobal(global_pos)
        if self.rect().contains(local_pos): return

        if self.config.get("ghost_mode", False):
            # 幽灵态下正文会变透明，高亮色块必须一起藏起来，
            # 否则"透明窗口上浮着几条彩色带子"，一眼就露馅
            self.ghost_veiled = True
            self.text_edit.setExtraSelections([])
            if self.config.get("auto_mode", False):
                self.chameleon_timer.stop()
                self.content_frame.set_draw_corners(False)
                current_bg = self.content_frame.auto_bg_fill
                r, g, b = current_bg.red(), current_bg.green(), current_bg.blue()
                ghost_bg_style = f"rgba({r}, {g}, {b}, 2)"
                self.text_edit.setStyleSheet(f"""
                    QTextEdit {{
                        color: transparent; 
                        background-color: {ghost_bg_style};
                        padding: 0px; margin: 0px; border: none;
                    }}
                """)
                self.setWindowOpacity(1.0)
            else:
                self.setWindowOpacity(0.005)
        super().leaveEvent(event)

    def fetch_bookshelf_silent(self):
        threading.Thread(target=self._fetch_bookshelf_thread, daemon=True).start()

    def _fetch_bookshelf_thread(self):
        try:
            url = f"{self.config['ip']}/getBookshelf"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                data = res.json()
                self.bookshelf_updated_signal.emit(data.get("data", []))
        except:
            pass

    def fetch_toc_silent(self, book_url):
        threading.Thread(target=self._fetch_toc_thread, args=(book_url,), daemon=True).start()

    def _fetch_toc_thread(self, book_url):
        try:
            url = f"{self.config['ip']}/getChapterList"
            res = requests.get(url, params={"url": book_url}, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data['isSuccess']:
                    self.current_toc = data['data']
        except:
            pass

    def open_book_selector(self):
        self.fetch_bookshelf_silent()
        self.book_selector_dialog = BookSelector(self, self)

        was_auto = self.config.get("auto_mode")
        if was_auto:
            self.setWindowOpacity(0.95)
            self.content_frame.setStyleSheet(f"background-color: {self.config['bg_color']};")
            self.content_frame.set_mode(False)

        if self.book_selector_dialog.exec_() == QDialog.Accepted:
            if self.book_selector_dialog.selected_book:
                self.load_book(self.book_selector_dialog.selected_book)

        self.apply_style()
        self.book_selector_dialog = None

    def open_weread_shelf(self):
        """📖 微信读书 · 我的书架（只到"读到哪了"，正文不做——见 weread.py）。"""
        if not WEREAD_OK:
            self.update_text_signal.emit(WEREAD_IMPORT_HINT, False)
            log_event(WEREAD_IMPORT_HINT)
            return
        client = weread_mod.build_weread(
            cache_root=os.path.join(APP_DIR, "cache"),
            root=APP_DIR, log=log_event)
        self.weread_dialog = WereadShelfDialog(client, self)
        self.weread_dialog.exec_()
        self.weread_dialog = None
        self.apply_style()

    def open_toc_selector(self):
        if not self.current_book:
            self.update_text_signal.emit("请先选择一本书！", False)
            return

        if not hasattr(self, 'current_toc') or self.current_toc is None:
            self.current_toc = []

        was_auto = self.config.get("auto_mode")
        if was_auto:
            self.setWindowOpacity(0.95)
            self.content_frame.setStyleSheet(f"background-color: {self.config['bg_color']};")
            self.content_frame.set_mode(False)

        toc = TocSelector(self.config['ip'], self.current_book['bookUrl'],
                          self.current_chapter_index, self.current_toc, self)

        if toc.exec_() == QDialog.Accepted:
            if toc.selected_index is not None:
                self.current_chapter_index = toc.selected_index
                self.update_text_signal.emit(f"跳转到章节: {self.current_chapter_index}", False)
                self.fetch_chapter_content(self.current_book['bookUrl'], self.current_chapter_index, False)

        self.apply_style()

    def load_book(self, book):
        self.is_local_mode = False  # 切换回网络模式
        self.current_book = book
        self.current_chapter_index = book.get('durChapterIndex', 0)
        self.current_toc = []
        self.update_text_signal.emit(f"打开: {book['name']}", False)
        self.fetch_chapter_content(book['bookUrl'], self.current_chapter_index, False)
        self.fetch_toc_silent(book['bookUrl'])

    def fetch_chapter_content(self, book_url, chapter_index, scroll_to_bottom=False):
        t = threading.Thread(target=self._fetch_chapter_thread,
                             args=(book_url, chapter_index, scroll_to_bottom), daemon=True)
        t.start()

    def _fetch_chapter_thread(self, book_url, chapter_index, scroll_to_bottom):
        try:
            chapter_title = ""
            if hasattr(self, 'current_toc') and self.current_toc:
                if 0 <= chapter_index < len(self.current_toc):
                    chapter_title = self.current_toc[chapter_index].get('title', '')

            if not chapter_title:
                chapter_title = f"第 {chapter_index + 1} 章"

            url = f"{self.config['ip']}/getBookContent"
            params = {'url': book_url, 'index': chapter_index}
            res = requests.get(url, params=params, timeout=5)

            if res.status_code == 200:
                data = res.json()
                if not data.get("isSuccess"):
                    self.update_text_signal.emit(f"读取失败: {data.get('errorMsg')}", False)
                    return

                raw_content = data.get("data", "")
                content = raw_content.replace("<br>", "\n").replace("&nbsp;", " ")

                full_text = f"【 {chapter_title} 】\n\n{content}"

                self.update_text_signal.emit(full_text, scroll_to_bottom)
                self.sync_progress_async()
            else:
                self.update_text_signal.emit(f"HTTP错误: {res.status_code}", False)
        except Exception as e:
            self.update_text_signal.emit(f"网络错误: {str(e)}", False)

    def sync_progress_async(self):
        if not self.current_book or self.is_local_mode or self._book_veiled(): return
        threading.Thread(target=self._sync_task, daemon=True).start()

    def _sync_task(self):
        try:
            title = ""
            if self.current_toc and 0 <= self.current_chapter_index < len(self.current_toc):
                title = self.current_toc[self.current_chapter_index].get("title", "")

            data = {
                "name": self.current_book['name'],
                "author": self.current_book['author'],
                "durChapterIndex": self.current_chapter_index,
                "durChapterPos": 0,
                "durChapterTime": int(time.time() * 1000),
                "durChapterTitle": title
            }
            url = f"{self.config['ip']}/saveBookProgress"
            requests.post(url, json=data, timeout=3)
        except:
            pass

    def next_chapter(self):
        # 【新增保护】防止 current_book 为 None
        if not self.current_book:
            return
        self.current_chapter_index += 1
        self.update_text_signal.emit("加载下一章...", False)
        self.fetch_chapter_content(self.current_book['bookUrl'], self.current_chapter_index, False)

    def prev_chapter(self):
        # 【新增保护】防止 current_book 为 None
        if not self.current_book:
            return
        if self.current_chapter_index > 0:
            self.current_chapter_index -= 1
            self.update_text_signal.emit("加载上一章...", False)
            self.fetch_chapter_content(self.current_book['bookUrl'], self.current_chapter_index, True)

    def is_in_resize_area(self, pos):
        rect = self.rect()
        resize_rect = QRect(rect.width() - self.resize_margin,
                            rect.height() - self.resize_margin,
                            self.resize_margin, self.resize_margin)
        return resize_rect.contains(pos)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.is_in_resize_area(event.pos()):
                self.is_resizing = True
                self.is_moving = False
            else:
                self.is_moving = True
                self.is_resizing = False
                self.oldPos = event.globalPos()

    def mouseMoveEvent(self, event):
        if self.is_in_resize_area(event.pos()):
            self.setCursor(Qt.SizeFDiagCursor)
        elif not self.is_resizing:
            self.setCursor(Qt.ArrowCursor)

        if event.buttons() == Qt.LeftButton:
            if self.is_resizing:
                # 先认住"现在正看的那一行"（用旧排版量），重排后让它仍然停在页首
                anchor = self.current_top_offset()
                new_w = max(event.pos().x(), 100)
                min_h = getattr(self, 'single_line_height', 20)
                new_h = max(event.pos().y(), min_h)
                self.resize(new_w, new_h)
                self.repage_after_resize(anchor)

            elif self.is_moving:
                delta = QPoint(event.globalPos() - self.oldPos)
                self.move(self.x() + delta.x(), self.y() + delta.y())
                self.oldPos = event.globalPos()

            if self.config.get("auto_mode"):
                self.adjust_color_to_background()

    def repage_after_resize(self, anchor=None, force=False):
        """缩放（以及改字号/改排版后）把**当前这一页**重排到新几何上。

        以前这里只会 `repage_to(anchor)`——那是给书页写的：卡片页的 `local_full_text`
        是空壳，`repage_to` 第一行就 return，于是卡片页缩放后 viewport 边距还停在旧高度。
        实测 429x787 → 429x460：viewport 从 512 掉到 185，下边距却还是 229（比可见高度
        还大），正文直接被挤没——这就是"缩放就出错"。

        拖动时每个鼠标事件都会进来，所以节流到 20 次/秒，松手时 force 收一次尾。
        """
        if not self.is_local_mode:
            return
        now = time.time()
        if not force and now - self._last_resize_repage < 0.05:
            return
        self._last_resize_repage = now
        if self.feed_mode:
            keep = self.text_edit.verticalScrollBar().value()
            self.render_feed()      # 宽度变了：卡片间距、图片宽度、页脚文字都跟着重排
            self.text_edit.verticalScrollBar().setValue(keep)
            return
        if self.review_mode and not self.safe_mode:
            keep = self.text_edit.verticalScrollBar().value()
            self.render_card()      # 里面会 apply_card_style() → apply_page_margins()，几何跟着新高度
            self.text_edit.verticalScrollBar().setValue(keep)
            self.fill_review_bar()  # 页脚文字跟着新宽度取舍（只重跑文字，不重套样式表）
            return
        self.repage_to(self.current_top_offset() if anchor is None else anchor)

    def mouseReleaseEvent(self, event):
        was_resizing = self.is_resizing
        self.is_resizing = False
        self.is_moving = False
        if was_resizing:
            self.repage_after_resize(force=True)   # 松手收尾：节流期间丢掉的那几像素补回来
        self.setCursor(Qt.ArrowCursor)
        self.save_config()

    # ================= 划线高亮 / 书签 =================
    def book_key(self):
        """当前书的存储键。网络书这轮不启用（正文会重新拉取，锚点不稳，不做半吊子）。"""
        if self.is_local_mode and self.local_file_path:
            return os.path.normcase(os.path.abspath(self.local_file_path))
        return None

    def book_store(self):
        key = self.book_key()
        if not key:
            return None
        store = self.annotations.get(key)
        if not isinstance(store, dict):
            store = {}
            self.annotations[key] = store
        store.setdefault("highlights", [])
        store.setdefault("bookmarks", [])
        return store

    def highlights_for_book(self):
        store = self.book_store()
        return sorted(store["highlights"], key=lambda h: int(h.get("start", 0))) if store else []

    def bookmarks_for_book(self):
        store = self.book_store()
        return sorted(store["bookmarks"], key=lambda b: int(b.get("pos", 0))) if store else []

    def persist_annotations(self):
        save_annotations(self.annotations)

    def chapter_at(self, offset):
        """这个偏移落在哪一章：返回 (标题, 层级)。epub 用目录，txt 用标题行兜底。"""
        title, level = "", 1
        for entry in (self.local_chapters or []):
            name, off = entry[0], entry[1]
            if int(off) <= offset:
                title = str(name).strip()
                level = int(entry[2]) if len(entry) > 2 else 1
            else:
                break
        if not title and self.local_heading_offsets:
            best = -1
            for off in self.local_heading_offsets:
                if off <= offset and off > best:
                    best = off
            if best >= 0:
                title = self.local_full_text[best:best + 40].split("\n")[0].strip()
        return title, max(1, int(level or 1))

    def chapter_title_at(self, offset):
        return self.chapter_at(offset)[0]

    def snippet_at(self, offset, length=60):
        return " ".join(self.local_full_text[offset:offset + length].split())

    def color_icon(self, name):
        pm = QPixmap(14, 14)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(highlight_color(name, 255))
        p.drawRoundedRect(1, 1, 12, 12, 3, 3)
        p.end()
        return QIcon(pm)

    def apply_highlight_selections(self):
        """把落在当前缓冲区里的划线画成 ExtraSelection。
        绝不动正文——正文一变，翻页的几何测量就跟着错（"已恢复进度"覆盖正文那个坑）。"""
        if self.feed_mode:
            # 社区页的选中高亮是它自己的 extra selection：这里一清，卡片上的选中标记就没了
            return
        if not (self.is_local_mode and self.local_full_text) or self.ghost_veiled or self._book_veiled():
            # 安全文档/背诵模式下一条高亮都不画：页面上那几条彩色横条比书页本身还露馅，
            # 而且在卡片上按卡的偏移画高亮，等于把书的划线挪到别的内容上
            self.text_edit.setExtraSelections([])
            return
        base = self.local_start_index
        buf_len = len(self.text_edit.toPlainText())
        buf_end = base + buf_len
        selections = []
        for hl in self.highlights_for_book():
            if hl.get("orphan"):
                continue
            try:
                s, e = int(hl["start"]), int(hl["end"])
            except Exception:
                continue
            if e <= base or s >= buf_end or e <= s:
                continue
            ls = max(s, base) - base
            le = min(min(e, buf_end) - base, buf_len)
            if le <= ls:
                continue
            cursor = QTextCursor(self.text_edit.document())
            cursor.setPosition(ls)
            cursor.setPosition(le, QTextCursor.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setBackground(highlight_color(hl.get("color", "yellow")))
            if hl.get("note"):
                # 有笔记的划线加一条虚线，作为"这里写着东西"的标记
                r, g, b = ANNOTATION_COLORS.get(hl.get("color", "yellow"), (255, 214, 102))
                fmt.setUnderlineStyle(QTextCharFormat.DashUnderline)
                fmt.setUnderlineColor(QColor(r, g, b, 255))
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = fmt
            selections.append(sel)
        self.text_edit.setExtraSelections(selections)

    def find_highlight_at(self, offset):
        """包含该偏移的划线；重叠时取最短的（最贴近点击意图）。"""
        if offset is None:
            return None
        hits = []
        for hl in self.highlights_for_book():
            if hl.get("orphan"):
                continue
            try:
                if int(hl["start"]) <= offset < int(hl["end"]):
                    hits.append(hl)
            except Exception:
                continue
        if not hits:
            return None
        return min(hits, key=lambda h: int(h["end"]) - int(h["start"]))

    def find_highlight_by_id(self, hid):
        for hl in self.highlights_for_book():
            if hl.get("id") == hid:
                return hl
        return None

    def create_highlight(self, start, end, text, color=None):
        store = self.book_store()
        if not store:
            return None
        color = color or self.highlight_default_color
        # 同一段文字重复确认只留一条：之前"点一下又划一次"就是这么冒出来的
        for hl in store["highlights"]:
            try:
                if int(hl.get("start", -1)) == int(start) and int(hl.get("end", -1)) == int(end):
                    hl["color"] = color
                    self.persist_annotations()
                    self.apply_highlight_selections()
                    return hl
            except Exception:
                continue
        hl = {"id": f"h{int(time.time() * 1000)}", "start": int(start), "end": int(end),
              "text": text, "color": color, "note": "",
              "created": time.strftime("%Y-%m-%d %H:%M")}
        store["highlights"].append(hl)
        self.persist_annotations()
        self.apply_highlight_selections()
        log_event(f"划线 +{end - start}字 @ {start}（{os.path.basename(self.local_file_path)}）")
        return hl

    def set_highlight_color(self, hid, color):
        hl = self.find_highlight_by_id(hid)
        if not hl:
            return
        hl["color"] = color
        self.highlight_default_color = color  # 用过的颜色就是下次默认
        self.persist_annotations()
        self.apply_highlight_selections()

    def edit_highlight_note(self, hid):
        hl = self.find_highlight_by_id(hid)
        if not hl:
            return
        self.is_popup_open = True
        try:
            text, ok = QInputDialog.getMultiLineText(self, "笔记", "为这条划线写点什么：",
                                                     hl.get("note", ""))
        finally:
            self.is_popup_open = False
        if ok:
            hl["note"] = text.strip()
            self.persist_annotations()
            self.apply_highlight_selections()

    def copy_highlight_text(self, hl):
        QApplication.clipboard().setText(hl.get("text", ""))
        QToolTip.showText(QCursor.pos(), "已复制划线原文", self)

    def delete_highlight(self, hid):
        store = self.book_store()
        hl = self.find_highlight_by_id(hid)
        if not (store and hl):
            return
        if hl.get("note"):
            # 带笔记的要二次确认，笔记删了找不回来
            self.is_popup_open = True
            try:
                ret = QMessageBox.question(
                    self, "取消划线",
                    "这条划线带笔记，一起删除吗？\n\n笔记：" + hl["note"][:200],
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            finally:
                self.is_popup_open = False
            if ret != QMessageBox.Yes:
                return
        store["highlights"] = [h for h in store["highlights"] if h.get("id") != hid]
        self.persist_annotations()
        self.apply_highlight_selections()

    def delete_bookmark(self, bid):
        store = self.book_store()
        if not store:
            return
        store["bookmarks"] = [b for b in store["bookmarks"] if b.get("id") != bid]
        self.persist_annotations()

    def toggle_bookmark(self):
        if self._book_veiled() or not (self.is_local_mode and self.local_full_text):
            return
        store = self.book_store()
        if not store:
            return
        pos = int(self.local_start_index)
        exist = next((b for b in store["bookmarks"] if int(b.get("pos", -1)) == pos), None)
        if exist:
            store["bookmarks"] = [b for b in store["bookmarks"] if b.get("id") != exist.get("id")]
            message = "已取消本页书签"
        else:
            store["bookmarks"].append({
                "id": f"b{int(time.time() * 1000)}", "pos": pos,
                "snippet": self.snippet_at(pos), "chapter": self.chapter_title_at(pos),
                "created": time.strftime("%Y-%m-%d %H:%M")})
            message = "已收藏本页（Ctrl+L 看书签）"
        self.persist_annotations()
        # 提示用浮动气泡，绝不往正文里写消息（写正文会毁掉翻页测量）
        QToolTip.showText(self.mapToGlobal(QPoint(self.width() // 2, 6)), message, self,
                          QRect(0, 0, 1, 1), 1600)
        log_event("书签: " + message)

    def validate_annotations(self):
        """换过书本文件时偏移会错位：按原文片段在 ±3000 字内找回，找不回的标记失效但不删。"""
        store = self.book_store()
        if not (store and self.local_full_text):
            return
        text = self.local_full_text
        changed = False
        for hl in store["highlights"]:
            snip = hl.get("text", "")
            try:
                s, e = int(hl["start"]), int(hl["end"])
            except Exception:
                hl["orphan"] = True
                changed = True
                continue
            if not snip or text[s:e] == snip:
                hl.pop("orphan", None)
                continue
            found = text.find(snip, max(0, s - 3000), s + 3000 + len(snip))
            if found < 0:
                found = text.find(snip)
            if found >= 0:
                hl["start"], hl["end"] = found, found + len(snip)
                hl.pop("orphan", None)
            else:
                hl["orphan"] = True
            changed = True
        for bm in store["bookmarks"]:
            snip = bm.get("snippet", "")
            try:
                pos = int(bm["pos"])
            except Exception:
                continue
            if not snip or " ".join(text[pos:pos + len(snip)].split()) == snip:
                continue
            found = text.find(snip, max(0, pos - 3000), pos + 3000 + len(snip))
            if found < 0:
                found = text.find(snip)
            if found >= 0:
                bm["pos"] = found
                changed = True
        if changed:
            self.persist_annotations()
            log_event("划线/书签位置已按原文校正")

    def jump_to_offset(self, offset):
        if not self.local_full_text:
            return
        self.local_page_history = []
        self.local_start_index = max(0, min(int(offset), max(0, len(self.local_full_text) - 1)))
        if not self._book_veiled():
            self.config["last_local_pos"] = self.local_start_index
            self.save_config()
        self.render_local_page()

    def open_reading_marks(self, initial_tab=0):
        if self._book_veiled() or not self.is_local_mode:
            return
        self.is_popup_open = True
        try:
            dialog = ReadingMarksDialog(self, initial_tab)
            if dialog.exec_() == QDialog.Accepted and dialog.jump_offset is not None:
                self.jump_to_offset(dialog.jump_offset)
            dialog.deleteLater()
        finally:
            self.is_popup_open = False

    def export_annotations(self):
        """导出成 Markdown：按章节分组，划线原文用引用块，笔记跟在后面。"""
        if self._book_veiled():
            return None
        if not (self.is_local_mode and self.local_file_path):
            QMessageBox.information(self, "导出", "先打开一本本地书。")
            return None
        highlights = self.highlights_for_book()
        bookmarks = self.bookmarks_for_book()
        if not highlights and not bookmarks:
            QMessageBox.information(self, "导出", "这本书还没有划线和书签。")
            return None
        book_name = os.path.splitext(os.path.basename(self.local_file_path))[0]
        out_path = os.path.join(os.path.dirname(self.local_file_path), f"{book_name}.划线笔记.md")
        noted = sum(1 for h in highlights if h.get("note"))
        lines = [f"# {book_name} · 划线笔记", "",
                 f"导出时间：{time.strftime('%Y-%m-%d %H:%M')}　"
                 f"划线 {len(highlights)} 条（带笔记 {noted} 条）、书签 {len(bookmarks)} 个", ""]
        if highlights:
            lines += ["## 划线", ""]
            last_chapter = None
            for hl in highlights:
                chapter, level = self.chapter_at(int(hl.get("start", 0)))
                chapter = chapter or "正文"
                if chapter != last_chapter:
                    # 目录有几级，导出的标题就有几级（### → #### → #####，最多到 ######）
                    lines += ["#" * min(6, 2 + max(1, level)) + f" {chapter}", ""]
                    last_chapter = chapter
                for line in str(hl.get("text", "")).strip().split("\n"):
                    lines.append("> " + line.strip())
                lines += [">", f"> —— {hl.get('created', '')}", ""]
                if hl.get("note"):
                    lines += [f"**笔记**：{hl['note']}", ""]
        if bookmarks:
            lines += ["## 书签", ""]
            for bm in bookmarks:
                lines.append(f"- {bm.get('chapter') or '正文'} · {bm.get('snippet', '')}"
                             f"　（{bm.get('created', '')}）")
            lines.append("")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return None
        log_event(f"导出划线笔记: {out_path}")
        QMessageBox.information(self, "导出完成", f"已导出到：\n{out_path}")
        return out_path

    # --- viewport 鼠标交互：划选后浮面板确认 / 点划线出面板 / 中键或 Alt+左键拖窗口 ---
    def buffer_offset_at(self, viewport_pos):
        """viewport 坐标 → 全文字符偏移。"""
        if not (self.is_local_mode and self.local_full_text):
            return None
        return self.local_start_index + self.text_edit.cursorForPosition(viewport_pos).position()

    def clear_text_selection(self):
        cursor = self.text_edit.textCursor()
        if cursor.hasSelection():
            cursor.clearSelection()
            self.text_edit.setTextCursor(cursor)

    def selection_anchor_global(self):
        """划选末端下方一点，作为面板的出场位置。"""
        cursor = self.text_edit.textCursor()
        pos = cursor.selectionEnd() if cursor.hasSelection() else cursor.position()
        probe = QTextCursor(self.text_edit.document())
        probe.setPosition(pos)
        rect = self.text_edit.cursorRect(probe)
        return self.text_edit.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom()))

    def on_viewport_press(self, event):
        if event.button() == Qt.MiddleButton or (event.button() == Qt.LeftButton
                                                 and event.modifiers() & Qt.AltModifier):
            # 正文区已经让给划选了，移动窗口改到这里：中键拖动，或 Alt+左键拖动
            self.filter_drag_origin = event.globalPos()
            self.filter_drag_window_pos = self.pos()
            self.setCursor(Qt.SizeAllCursor)
            self.text_edit.set_pinned_cursor(Qt.SizeAllCursor)
            return True
        if event.button() == Qt.LeftButton:
            # 先把上一轮残留的选中清掉，不然它会被当成一次"新划选"再确认一遍（重复划线就是这么来的）
            self.clear_text_selection()
        if event.button() == Qt.RightButton:
            # 安全文档/背诵模式下不认划线：这两份内容本来就没有任何标注
            hit = None if self._book_veiled() else self.find_highlight_at(self.buffer_offset_at(event.pos()))
            if hit:
                self.show_action_panel("existing", hl=hit,
                                       anchor=self.text_edit.viewport().mapToGlobal(event.pos()))
                return True
        return False

    def on_viewport_move(self, event):
        if self.filter_drag_origin is not None:
            delta = event.globalPos() - self.filter_drag_origin
            self.move(self.filter_drag_window_pos + delta)
            return True
        if self.feed_mode:
            # 列表是"单击就进"，所以鼠标一停在哪条上就该开始取那条（不用等人按下去）
            post = self.feed_post_at(event.pos())
            if post is not None:
                self.feed_warm_soon(post)
        return False

    def on_viewport_release(self, event):
        if self.filter_drag_origin is not None:
            self.filter_drag_origin = None
            self.setCursor(Qt.ArrowCursor)
            self.text_edit.set_pinned_cursor(Qt.ArrowCursor)   # 拖动结束，正文区回箭头
            self.save_window_pos()
            return True
        if self.feed_mode:
            # 社区页：列表单击就进详情、评论单击就折叠/展开（见 feed_click）。划到了字就仍然算"选字复制"，
            # 交给原来的划选路径——工位上想抄一段走，这条不能堵。
            if (event.button() == Qt.LeftButton
                    and not self.text_edit.textCursor().hasSelection()
                    and self.feed_click(event.pos())):
                self.clear_text_selection()   # 点选之后不留光标/残影
                return True
            return False
        if self.review_mode:
            # 背诵时：单击一下（没划到字）＝ 翻面，和 Anki 里点卡片同义；拖选只用来复制
            if event.button() == Qt.LeftButton and not self.text_edit.textCursor().hasSelection():
                self.review_show_answer()
            return False
        if event.button() != Qt.LeftButton or not (self.is_local_mode and self.local_full_text):
            return False
        if self._book_veiled():
            return False   # 安全文档/背诵模式下可以选中、可以复制，但不会划线也不会弹面板
        cursor = self.text_edit.textCursor()
        if cursor.hasSelection():
            # 一律用文档位置取偏移和原文，不用 selectedText（Qt 拿 U+2029 当段落分隔符，长度会对不上）
            ls, le = cursor.selectionStart(), cursor.selectionEnd()
            snippet = self.text_edit.toPlainText()[ls:le]
            if len(snippet.strip()) >= 2:
                # 不直接划线：先浮出面板等确认；滚轮/点别处/按键都不算确认
                self.pending_selection = (self.local_start_index + ls,
                                          self.local_start_index + le, snippet)
                self.show_action_panel("new", anchor=self.selection_anchor_global())
                return False
        # 没选到东西、或只带出一个字/一块空白：都按"单击"办
        # （点的时候手指抖一下会留下一个字的残留选中，不能因此把菜单吞了）
        hit = self.find_highlight_at(self.buffer_offset_at(event.pos()))
        if hit:
            pos = QPoint(event.pos())
            QTimer.singleShot(0, lambda h=hit, p=pos: self.show_action_panel(
                "existing", hl=h, anchor=self.text_edit.viewport().mapToGlobal(p)))
        return False

    def show_action_panel(self, mode, hl=None, anchor=None):
        # 换新面板时不能作废 pending——"新划选"的 pending 是刚设好的（这个坑踩过一次）
        self.dismiss_action_panel(cancelled=False)
        if mode == "existing":
            self.pending_selection = None
        hl = hl or {}
        current = hl.get("color") or self.highlight_default_color
        panel = HighlightActionPanel(self, mode=mode, current_color=current,
                                     has_note=bool(hl.get("note")))
        panel.chosen.connect(lambda key: self.on_panel_action(key, mode, hl))
        panel.cancelled.connect(lambda: self.on_panel_cancelled(panel))
        panel.wheel_scrolled.connect(self.on_panel_wheel)
        self.action_panel = panel
        self.is_popup_open = True

        panel.adjustSize()
        gpos = anchor or QCursor.pos()
        screen = QApplication.screenAt(gpos) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        x = min(max(gpos.x(), avail.left() + 4), avail.right() - panel.width() - 4)
        y = gpos.y() + 6
        if y + panel.height() > avail.bottom() - 4:
            y = gpos.y() - panel.height() - 10   # 下面放不下就翻到上方
        panel.move(max(avail.left() + 4, x), max(avail.top() + 4, y))
        # 弹层要求窗口是激活的，不然 Qt 会把 popup 立刻收掉（用户刚在这里点过，激活是安全的）
        self.activateWindow()
        panel.show()

    def dismiss_action_panel(self, cancelled=False):
        panel, self.action_panel = self.action_panel, None
        if panel is not None:
            panel.hide()
            panel.deleteLater()
        if cancelled:
            # 不确认：连选中一起清掉，免得那个残留选中下次被当成新划选
            self.pending_selection = None
            self.clear_text_selection()
        if self.action_panel is None:
            self.is_popup_open = False

    def on_panel_cancelled(self, panel=None):
        # 点别处 / Esc / 窗口藏起来 → 面板自己 hide，这里负责收尾
        # 注意要真的销毁控件：只清引用的话，弹层窗口会一个个攒下来（实测踩过）
        # 另外：旧面板 hide 时也会发 cancelled，而且这个 hideEvent 是延迟送达的——等它到的时候
        # self.action_panel 往往已经换成新面板了（刚拖出来的划选）。只认发信号的那个面板，
        # 否则会把新选区连新面板一起清掉：表现就是"划线时好时不好"。
        if panel is not None and panel is not self.action_panel:
            return
        panel = self.action_panel
        self.action_panel = None
        if panel is not None:
            panel.hide()
            panel.deleteLater()
        self.pending_selection = None
        self.clear_text_selection()
        self.is_popup_open = False

    def on_panel_wheel(self, delta):
        """滚轮：视为不确认，但滚轮本身照样滚动（手动把这一下补回去）。"""
        self.dismiss_action_panel(cancelled=True)
        try:
            self._local_wheel_step(delta)
        except Exception:
            pass

    def confirm_pending_highlight(self, color):
        if not self.pending_selection:
            return None
        start, end, text = self.pending_selection
        self.pending_selection = None
        hl = self.create_highlight(start, end, text, color)
        self.highlight_default_color = color
        self.clear_text_selection()
        return hl

    def on_panel_action(self, key, mode, hl):
        if key == "default":
            self.confirm_pending_highlight(self.highlight_default_color)
            self.dismiss_action_panel()
        elif key.startswith("color:"):
            color = key.split(":", 1)[1]
            if mode == "new":
                self.confirm_pending_highlight(color)
                self.dismiss_action_panel()
            else:
                self.set_highlight_color(hl.get("id"), color)
                self.dismiss_action_panel()
        elif key == "note":
            if mode == "new":
                created = self.confirm_pending_highlight(self.highlight_default_color)
                self.dismiss_action_panel()
                if created:
                    self.edit_highlight_note(created["id"])
            else:
                self.dismiss_action_panel()
                self.edit_highlight_note(hl.get("id"))
        elif key == "copy":
            text = (self.pending_selection[2] if (mode == "new" and self.pending_selection)
                    else hl.get("text", ""))
            QApplication.clipboard().setText(text)
            QToolTip.showText(QCursor.pos(), "已复制原文", self)
            self.dismiss_action_panel(cancelled=(mode == "new"))
        elif key == "delete":
            self.dismiss_action_panel()
            self.delete_highlight(hl.get("id"))

    def build_feed_menu(self):
        """社区页的右键菜单。

        为什么要这份菜单：`[` `]` `,` `.` 这三个键**没有地方能看见**——真人反馈"没法切换不同社区"
        就是这个原因（功能一直在，但没人知道键位）。菜单里放同名的两项，另外给两个可以打字搜的
        选择器（来源一共七八个、NGA 的板块有六百多个，菜单塞不下）。
        """
        cmenu = QMenu(self)
        nav = self.feed_nav
        src = self.feed_source()
        copy_action = cmenu.addAction("📋 复制选中文字")
        copy_action.triggered.connect(self.copy_selection)
        copy_action.setEnabled(bool(self.text_edit.textCursor().hasSelection()))
        cmenu.addSeparator()
        # 换社区（来源）
        src_menu = cmenu.addMenu("🌐 换社区（%s）" % self.feed_source_label())
        for i, s in enumerate(self.feed_sources):
            label = getattr(s, "label", "") or getattr(s, "key", "")
            act = src_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(i == self.feed_source_i)
            act.triggered.connect(lambda _=False, idx=i: self.feed_goto_source(idx))
        src_menu.addSeparator()
        src_menu.addAction("▶️ 下一个社区（C）").triggered.connect(
            lambda: self.feed_cycle_source(1))
        src_menu.addAction("🔍 选一个社区…").triggered.connect(self.feed_source_picker)
        # 换板块（栏目）
        try:
            channels = list(src.channels() or []) if src is not None else []
        except Exception:
            channels = []
        cur = nav.channel if nav is not None else ""
        board_menu = cmenu.addMenu("📋 换板块（%s）" % (cur or "—"))
        for ch in channels[:24]:
            act = board_menu.addAction(ch)
            act.setCheckable(True)
            act.setChecked(ch == cur)
            act.triggered.connect(lambda _=False, c=ch: self.feed_goto_channel(c))
        if len(channels) > 24:
            board_menu.addSeparator()
            board_menu.addAction("　…还有 %d 个（用下面的列表搜）" % (len(channels) - 24)).setEnabled(False)
        board_menu.addSeparator()
        board_menu.addAction("▶️ 下一个板块（V）").triggered.connect(
            lambda: self.feed_cycle_channel(1))
        board_menu.addAction("🔍 选一个板块…").triggered.connect(self.feed_board_picker)
        cmenu.addSeparator()
        cmenu.addAction("🔄 重新拉取（R）").triggered.connect(self.feed_reload)
        if nav is not None and nav.view == "detail":
            _view_label = self.feed_view_label()
            if _view_label:
                cmenu.addAction(_view_label).triggered.connect(self.feed_toggle_view)
            cmenu.addAction("⬅️ 返回列表（退格）").triggered.connect(self.feed_back)
        cmenu.addAction("↗️ 用浏览器打开原帖（O）").triggered.connect(self.feed_open_original)
        cmenu.addSeparator()
        cmenu.addAction("⚙️ 设置").triggered.connect(self.open_settings)
        cmenu.addAction("❌ 退出社区页（%s）" % (self.config.get("feed_key") or "ctrl+alt+f")).triggered.connect(
            lambda: self.exit_feed_mode(reason="menu"))
        return cmenu

    def build_context_menu(self):
        """右键菜单。安全文档模式下只给中性项：菜单里的"打开本地书籍/导出划线笔记"这类字眼
        本身就是破绽，凑过来看屏幕的人一眼就懂了。"""
        cmenu = QMenu(self)
        if self.feed_mode:
            # 社区页有自己的菜单：换社区/换板块/刷新/回列表/开原帖。
            # 走书页那份菜单会露出"打开本地书籍""导出划线笔记"这些和当前页面无关的项。
            return self.build_feed_menu()
        if self.review_mode:
            # 背诵菜单：也要中性（不出现 "Anki/牌组/评分" 这类字眼），但鼠标路径必须完整——
            # 万一设置里关掉了吞键，或者有人习惯只用鼠标，也能翻面、评分、退出。
            s = self.review_session or {}
            if s.get("shown"):
                for btn in (s.get("card") or {}).get("buttons") or [1, 2, 3, 4]:
                    cmenu.addAction(f"　{btn}　").triggered.connect(
                        lambda _=False, b=btn: self.review_grade_button(b))
            else:
                cmenu.addAction("　翻面　").triggered.connect(self.review_show_answer)
            cmenu.addSeparator()
            cmenu.addAction("　重放音频　").triggered.connect(self.review_play_audio)
            cmenu.addAction("　撤销　").triggered.connect(self.review_undo)
            cmenu.addSeparator()
            copy_action = cmenu.addAction("📋 复制选中文字")
            copy_action.triggered.connect(self.copy_selection)
            copy_action.setEnabled(bool(self.text_edit.textCursor().hasSelection()))
            cmenu.addSeparator()
            cmenu.addAction("　收起卡片　").triggered.connect(
                lambda: self.exit_review_mode(reason="menu"))
            cmenu.addAction("⚙️ 设置").triggered.connect(self.open_settings)
            cmenu.addSeparator()
            cmenu.addAction("❌ 退出").triggered.connect(self.quit_app)
            return cmenu
        if self._book_veiled():
            copy_action = cmenu.addAction("📋 复制选中文字")
            copy_action.triggered.connect(self.copy_selection)
            copy_action.setEnabled(bool(self.text_edit.textCursor().hasSelection()))
            cmenu.addSeparator()
            cmenu.addAction("⚙️ 设置").triggered.connect(self.open_settings)
            cmenu.addSeparator()
            cmenu.addAction("❌ 退出").triggered.connect(self.quit_app)
            return cmenu
        cmenu.addAction("📂 打开本地书籍 (TXT/EPUB)").triggered.connect(self.open_local_file_dialog)
        cmenu.addAction("🃏 隐形背诵（Anki 卡片）").triggered.connect(self.toggle_review_mode)
        cmenu.addAction("🌐 社区统一流（知乎/V2EX/微博…）").triggered.connect(self.toggle_feed_mode)
        marks_action = cmenu.addAction("📑 目录 / 书签 / 划线 (Ctrl+L)")
        marks_action.triggered.connect(lambda: self.open_reading_marks(0))
        marks_action.setEnabled(bool(self.is_local_mode))
        mark_here = cmenu.addAction("🔖 收藏本页 / 取消书签 (Ctrl+B)")
        mark_here.triggered.connect(self.toggle_bookmark)
        mark_here.setEnabled(bool(self.is_local_mode))
        export_action = cmenu.addAction("📤 导出划线笔记 (Markdown)")
        export_action.triggered.connect(self.export_annotations)
        export_action.setEnabled(bool(self.is_local_mode))
        copy_action = cmenu.addAction("📋 复制选中文字")
        copy_action.triggered.connect(self.copy_selection)
        copy_action.setEnabled(bool(self.text_edit.textCursor().hasSelection()))
        cmenu.addSeparator()
        cmenu.addAction("📚 网络书架 (搜索)").triggered.connect(self.open_book_selector)
        cmenu.addAction("📖 微信读书 · 我的书架").triggered.connect(self.open_weread_shelf)
        cmenu.addAction("📖 章节目录 (网络)").triggered.connect(self.open_toc_selector)
        cmenu.addSeparator()
        cmenu.addAction("⚙️ 设置").triggered.connect(self.open_settings)
        cmenu.addSeparator()
        cmenu.addAction("❌ 退出").triggered.connect(self.quit_app)
        return cmenu

    def contextMenuEvent(self, event):
        cmenu = self.build_context_menu()
        modernize_menu(cmenu)
        cmenu.exec_(self.mapToGlobal(event.pos()))
        cmenu.deleteLater()   # 菜单挂在窗口上，不销毁会一次右键攒一个

    def copy_selection(self):
        cursor = self.text_edit.textCursor()
        if cursor.hasSelection():
            QApplication.clipboard().setText(cursor.selectedText().replace("\u2029", "\n"))
            QToolTip.showText(QCursor.pos(), "已复制选中文字", self)

    def open_local_toc(self):
        # 老入口保留，直接开三合一的目录页
        self.open_reading_marks(0)

    def apply_feed_sources(self):
        """设置里改了"哪些来源生效"：就地重建来源列表，**保住当前这个来源**。

        不重载整本书、也不退出社区页；只是把 `feed_sources` 换一份新的。
        当前那个来源要是被关掉了，就落到第一个（混流）；正在看社区页的话重新拉一页。
        """
        if not FEED_OK:
            return
        try:
            cur = getattr(self.feed_source(), "key", "")
        except Exception:
            cur = ""
        try:
            self.feed_sources = feed_mod.build_sources(
                os.path.join(RES_DIR, "feed_demo"),
                cache_root=os.path.join(APP_DIR, "cache", "feed"),
                config=self.config, log=log_event)
        except Exception as exc:
            log_event("重建社区来源失败: %s" % exc)
            return
        keys = [getattr(src, "key", "") for src in self.feed_sources]
        self.feed_source_i = keys.index(cur) if cur in keys else 0
        self._feed_memo.clear()
        self._feed_sig = None
        if self.feed_mode and self.feed_nav is not None:
            self.feed_nav.reset()
            self.feed_nav.channel = self.feed_first_channel()
            self.render_feed()
            self.feed_load("list")
        log_event("社区来源：%d 个（当前 %s）" % (len(self.feed_sources),
                                                getattr(self.feed_source(), "key", "")))

    def open_settings(self):
        self.is_settings_open = True
        was_auto = self.config.get("auto_mode")
        if was_auto:
            self.content_frame.set_mode(False)
            self.setWindowOpacity(0.95)
            self.content_frame.setStyleSheet(f"background-color: {self.config['bg_color']};")

        dialog = SettingsDialog(self.config, self)

        if dialog.exec_() == QDialog.Accepted:
            self.config = dialog.config
            self.save_config()
            self.apply_style()
            self.apply_feed_sources()
            self.refresh_hotkeys()
            if self.config["ip"].startswith("http"):
                self.fetch_bookshelf_silent()
        else:
            self.apply_style()

        self.is_settings_open = False
        self.showNormal()
        self.activateWindow()

    def keyPressEvent(self, event):
        key = event.key()
        if self.feed_mode:
            # 社区页的键全在 on_feed_key 里；这里吃掉没处理的键，别让书页那套切页逻辑
            # 把社区流当成书来翻（两者是不同页面，滚动方式也不一样）
            if self.on_feed_key(key):
                event.accept()
            return
        if self.review_mode and not self.safe_mode:
            # 卡片页的交互键 = **窗口内**（和书页完全同一套机制）：只有窗口是当前活动窗口
            # 时才响应。在这之前这套键走的是全局低级钩子，你在别的软件里打字按到 1-4
            # 会把卡片直接评掉——那条路已经删掉了。
            name = REVIEW_KEY_FROM_QT.get(key)
            if name is not None:
                self.on_review_key(name)
                event.accept()
                return
            # 其余方向键/翻页键滚的是卡片自己（卡片可能比窗口高），不翻书页
            sb = self.text_edit.verticalScrollBar()
            page = max(1, self.text_edit.viewport().height())
            if key in [Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown]:
                sb.setValue(min(sb.maximum(), sb.value() + page))
            elif key in [Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp]:
                sb.setValue(max(sb.minimum(), sb.value() - page))
            return
        if key in (Qt.Key_Down, Qt.Key_Up):
            # 上下箭头 = 逐行滚动（行数可调），和滚轮一个手感；
            # 整页翻留给空格/PageDown/PageUp/左右箭头
            self.scroll_lines(1 if key == Qt.Key_Down else -1)
        elif key in [Qt.Key_Right, Qt.Key_Space, Qt.Key_PageDown]:
            self.scroll_page(1)
        elif key in [Qt.Key_Left, Qt.Key_PageUp]:
            self.scroll_page(-1)

    def closeEvent(self, event):
        self.exit_peek("close")  # 窗口关了必须先卸全局钩子，再走保存
        if self.review_mode:
            self.exit_review_mode(reason="close")   # 关窗口＝整套收起（只想藏卡片用 Ctrl+D）
        if self.feed_mode:
            self.exit_feed_mode(reason="close")     # 社区页也收起：书页按快照回来
        self.shutdown_anki()     # X 关窗是最常用的退出方式：这里也必须把后台线程停干净
        self.dismiss_action_panel(cancelled=True)
        self.sync_progress_async()
        self.save_window_pos()
        if self.is_local_mode and not self._book_veiled():
            self.config["last_local_pos"] = self.current_top_offset()   # 视口顶那一行，不是页首
            self.save_config()
        super().closeEvent(event)

    def quit_app(self):
        # 退出前强制保存本地进度（安全文档/背诵/社区模式下不存：那些坐标不是书的进度）
        if self.feed_mode:
            self.exit_feed_mode(reason="quit")
        if self.is_local_mode and not self._book_veiled():
            self.config["last_local_pos"] = self.current_top_offset()
            self.save_config()
        self.save_window_pos()

        self.peek_input_hook.uninstall()  # 钩子泄漏的症状是全系统滚轮发卡，退出必须拆干净
        keyboard.unhook_all()
        self.shutdown_anki()              # 放 Anki 出来 + 停后台线程（线程没停就退会崩）
        try:
            ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(int(self.winId()))
        except Exception:
            pass
        QApplication.instance().quit()


if __name__ == '__main__':
    # 单实例：双开会导致两个进程同时响应老板键（看起来像"唤醒不了"），并且互相覆盖
    # config 里的阅读进度（看起来像"进度跳位"）。用命名互斥体挡住第二个进程。
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "StealthReader_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "StealthReader 已经在运行了（托盘里）", "提示", 0x40)
        sys.exit(0)

    # 防静默死亡：PyQt5 对槽函数里未处理的异常默认 qFatal 直接杀进程（pythonw 无控制台，
    # 死得无声无息）。装了自定义 excepthook 后 PyQt5 改为只调钩子不杀进程，异常落进日志，
    # 应用继续跑。
    def _unhandled_exc(t, v, tb):
        try:
            log_event("未处理异常:\n" + "".join(traceback.format_exception(t, v, tb))[-900:])
        except Exception:
            pass
    sys.excepthook = _unhandled_exc

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ex = StealthReader()
    ex.show()
    # 启动恢复位置后校验一次：窗口可能被存到了已经不存在的显示器坐标上
    QTimer.singleShot(400, ex.ensure_on_screen)
    log_event("应用启动")
    sys.exit(app.exec_())