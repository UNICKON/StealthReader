import sys
import os

# 自带依赖目录（PyQt5 / keyboard 都解包在 libs/）：不依赖启动器设置 PYTHONPATH，
# 任何方式拉起本文件都能活。必须放在 import keyboard 之前。
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
                             QInputDialog, QMessageBox, QToolTip, QTabWidget)
from PyQt5.QtCore import (Qt, QPoint, QRect, QPointF, QSize, pyqtSignal, QObject,
                          QThread, QTimer, QEvent, QUrl)
from PyQt5.QtGui import (QFont, QColor, QCursor, QKeySequence, QPainter, QPen,
                         QFontMetrics, QTextCursor, QTextBlockFormat, QWheelEvent,
                         QPixmap, QIcon, QPainterPath, QTextCharFormat)

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

# 启用高分屏支持
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

CONFIG_FILE = "config.json"

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
    不经过 Qt——Qt 会缓存过期屏幕布局，拔过显示器后会把窗口安排到不存在的屏幕坐标里。"""
    try:
        user32 = ctypes.windll.user32
        return bool(user32.MonitorFromWindow(hwnd, 0))  # MONITOR_DEFAULTTONULL
    except Exception:
        return True  # 校验失败时别乱动窗口


def force_window_to_primary(hwnd, margin=60):
    """Win32 物理挪回主屏工作区左上角（绕过 Qt 可能过期的坐标映射，挪完 Qt 会自己同步回来）。"""
    try:
        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromWindow(hwnd, 1)  # MONITOR_DEFAULTTOPRIMARY
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if not hmon or not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        x, y = mi.rcWork[0] + margin, mi.rcWork[1] + margin
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
        user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return True
    except Exception:
        return False


# ================= 轻量事件日志（排查老板键之类"偶尔失灵"的问题）=================
def log_event(msg):
    """追加一行到 main.py 旁边的 stealthreader.log，失败静默（日志不能反过来影响功能）。"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stealthreader.log")
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
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "annotations.json")


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
# 设置里关掉"连空格/回车也吞"之后，仍然要吞这些（评分与退出必须能收到）
REVIEW_KEY_NAMES_MIN = {"1", "2", "3", "4", "u", "r", "esc"}

# 评分按钮的名字：Anki 按张数变（学习步/FSRS 下可能只有 2 个按钮）
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
    """偷瞄模式的全局输入钩子：滚轮翻页（吞掉，底下应用不滚），
    右键/中键/侧键与按键退出（放行），左键放行且不退出（偷瞄时要能照常点工作窗口）。

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
                # 隐形背诵：只吞那 9 个复习键，而且只在"背诵中 + 窗口可见 + 没最小化"时吞
                # （想正常打字就先 Ctrl+D 把窗口收起来）。其余按键一律放行。
                if getattr(self.owner, "review_mode", False) and self.owner.review_keys_active():
                    name = REVIEW_KEY_NAMES.get(int(info.vkCode))
                    if name and (self.owner.review_swallow_all() or name in REVIEW_KEY_NAMES_MIN):
                        self.owner.review_key_signal.emit(name)
                        return 1
                    return 0
                if self.owner.peek_mode:
                    if self._matches_peek_key(int(info.vkCode)):
                        self.owner.peek_exit_signal.emit("peek_key")
                        return 1  # 偷瞄期间这个键归我们，别落到当前应用上
                    self.owner.peek_exit_signal.emit("key")
                    return 0  # 其他按键放行
        except Exception:
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)


# ================= 辅助类：绘制背景和角标 =================
class CornerFrame(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_auto_mode = False
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
        self.update()

    def paintEvent(self, event):
        if not self.is_auto_mode:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.auto_bg_fill)

        if self.height() > 20:
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
        self.setWindowTitle("📚 书架")
        self.resize(400, 500)
        self.setStyleSheet(DARK_STYLESHEET)
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
             "page_margin_chars", "first_line_indent", "paragraph_indent")
        }

        self.setWindowTitle("设置")
        self.resize(350, 560)
        self.setStyleSheet(DARK_STYLESHEET)
        self.initUI()

    def initUI(self):
        layout = QFormLayout()
        self.ip_input = QLineEdit(self.config.get("ip"))
        layout.addRow("Legado地址:", self.ip_input)

        self.check_auto_mode = QCheckBox("🦎 自动挡 (变色龙)")
        self.check_auto_mode.setToolTip("开启后，背景变为背景色+极低透明度。\n字体颜色自动反转。")
        self.check_auto_mode.setChecked(self.config.get("auto_mode", False))
        self.check_auto_mode.toggled.connect(self.on_auto_mode_toggled)
        layout.addRow(self.check_auto_mode)

        self.check_antishot = QCheckBox("🛡️ 系统级防截屏")
        self.check_antishot.setToolTip("开启后，肉眼可见，但截图/录屏时窗口会完全消失（透明）。\n使用 Windows 系统底层保护。")
        self.check_antishot.setChecked(self.config.get("antishot_mode", False))
        self.check_antishot.toggled.connect(self.on_antishot_toggled)
        layout.addRow(self.check_antishot)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(10, 100)
        self.opacity_slider.setValue(int(self.config.get("opacity") * 100))
        self.opacity_slider.valueChanged.connect(self.on_opacity_change)
        layout.addRow("不透明度:", self.opacity_slider)

        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 60)
        self.font_spin.setValue(self.config.get("font_size"))
        self.font_spin.valueChanged.connect(self.on_font_change)
        layout.addRow("字体大小:", self.font_spin)

        self.font_combo = QFontComboBox()
        current_font_family = self.config.get("font_family", "Microsoft YaHei")
        self.font_combo.setCurrentFont(QFont(current_font_family))
        self.font_combo.currentFontChanged.connect(self.on_font_change)
        layout.addRow("字体样式:", self.font_combo)

        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(LINE_SPACING_MIN, LINE_SPACING_MAX)
        self.spacing_spin.setSingleStep(5)
        self.spacing_spin.setSuffix(" %")
        self.spacing_spin.setValue(clamp_config_int(self.config, "line_spacing", LINE_SPACING_DEFAULT,
                                                    LINE_SPACING_MIN, LINE_SPACING_MAX))
        self.spacing_spin.setToolTip("行距 = 行高的百分比。100% 为原始行距，150% 约等于一行字配半行空白。")
        self.spacing_spin.valueChanged.connect(self.on_typography_change)
        layout.addRow("行距:", self.spacing_spin)

        self.para_spin = QSpinBox()
        self.para_spin.setRange(PARA_SPACING_MIN, PARA_SPACING_MAX)
        self.para_spin.setSingleStep(10)
        self.para_spin.setSuffix(" %")
        self.para_spin.setValue(clamp_config_int(self.config, "paragraph_spacing", 0,
                                                 PARA_SPACING_MIN, PARA_SPACING_MAX))
        self.para_spin.setToolTip("段与段之间的额外空白，同样以行高为基准，0% 为原始效果。\n"
                                  "注意：若 txt 里每行都是硬换行，段间距会看起来像额外的行距。")
        self.para_spin.valueChanged.connect(self.on_typography_change)
        layout.addRow("段间距:", self.para_spin)

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
        self.margin_spin.setToolTip("文字四周的留白（上下左右相同），单位是「字」——1 字 = 一个全角空格宽，\n"
                                    "随字号字体等比变化（字号调大，留白跟着变宽）。\n"
                                    "窗口很窄时别设太大，会挤掉每行字数。")
        self.margin_spin.valueChanged.connect(self.on_typography_change)
        layout.addRow("页边距:", self.margin_spin)

        self.para_indent_check = QCheckBox("段落缩进")
        self.para_indent_check.setChecked(bool(self.config.get("paragraph_indent", True)))
        self.para_indent_check.setToolTip("给每个自然段的第一行加缩进；标题（章节名、序、前言这类行）自动不缩进。\n"
                                          "不想要缩进也可以把下面的「首行缩进」调到 0。")
        self.para_indent_check.toggled.connect(self.on_typography_change)
        layout.addRow(self.para_indent_check)

        self.indent_spin = QDoubleSpinBox()
        self.indent_spin.setRange(FIRST_LINE_INDENT_MIN, FIRST_LINE_INDENT_MAX)
        self.indent_spin.setSingleStep(0.5)
        self.indent_spin.setDecimals(1)
        self.indent_spin.setSuffix(" 字")
        self.indent_spin.setValue(clamp_config_number(self.config, "first_line_indent",
                                                      FIRST_LINE_INDENT_DEFAULT,
                                                      FIRST_LINE_INDENT_MIN, FIRST_LINE_INDENT_MAX))
        self.indent_spin.setToolTip("缩进多少，1 字符 = 一个全角空格的宽度（随字号字体变化）。\n"
                                    "0 就是不缩进。")
        self.indent_spin.valueChanged.connect(self.on_typography_change)
        layout.addRow("首行缩进:", self.indent_spin)

        self.btn_text_color = QPushButton("文字颜色 (手动)")
        self.btn_text_color.setStyleSheet(f"background-color: {self.temp_text_color};")
        self.btn_text_color.clicked.connect(self.pick_text_color)
        self.btn_bg_color = QPushButton("背景颜色 (手动)")
        self.btn_bg_color.setStyleSheet(f"background-color: {self.temp_bg_color};")
        self.btn_bg_color.clicked.connect(self.pick_bg_color)
        layout.addRow(self.btn_text_color, self.btn_bg_color)

        self.check_ghost_mode = QCheckBox("👻 幽灵模式 (移开变透明)")
        self.check_ghost_mode.setChecked(self.config.get("ghost_mode", False))
        layout.addRow(self.check_ghost_mode)

        self.boss_key_input = QLineEdit(self.config.get("boss_key", "Esc"))
        layout.addRow("全局老板键:", self.boss_key_input)
        self.peek_key_input = QLineEdit(self.config.get("peek_key", "ctrl+e"))
        self.peek_key_input.setToolTip("偷瞄模式：按这个键浮现书页，鼠标滚轮在任何位置都能翻页，\n"
                                       "敲键盘 / 右键 / 中键则退出并藏回去；\n"
                                       "左键点击不退出（偷瞄时能照常点工作窗口）。热键本身不会被发给当前应用。")
        layout.addRow("偷瞄热键:", self.peek_key_input)

        self.safe_key_input = QLineEdit(self.config.get("safe_key", "ctrl+alt+d"))
        self.safe_key_input.setToolTip("安全文档模式：有人凑过来看屏幕时按这个键，书页立刻换成一份预置的工作文档，\n"
                                       "排版（字体/行距/页边距/窗口大小）和书页完全一致；\n"
                                       "再按一次换回书，回到原来那一页。划线会一起隐藏。")
        layout.addRow("安全文档热键:", self.safe_key_input)

        safe_row = QWidget()
        safe_layout = QHBoxLayout(safe_row)
        safe_layout.setContentsMargins(0, 0, 0, 0)
        self.safe_doc_input = QLineEdit(self.config.get("safe_doc_path", ""))
        self.safe_doc_input.setPlaceholderText("留空 = 用内置的工作周报体例")
        self.safe_doc_input.setToolTip("可选：指定本机的一份文档当安全文档（TXT / MD / EPUB）。\n"
                                       "读不出来会自动回落内置版——任何情况下都不会露出书页。")
        btn_pick_safe = QPushButton("选择…")
        btn_pick_safe.clicked.connect(self.pick_safe_doc)
        safe_layout.addWidget(self.safe_doc_input)
        safe_layout.addWidget(btn_pick_safe)
        layout.addRow("安全文档文件:", safe_row)

        # --- 隐形背诵（Anki）：只是把 Anki 的卡片借这本书的排版显示出来 ---
        section = QLabel("── 隐形背诵（Anki 卡片）──")
        section.setStyleSheet("color: #8ab4f8;")
        layout.addRow(section)

        self.anki_key_input = QLineEdit(self.config.get("anki_key", "ctrl+alt+a"))
        self.anki_key_input.setToolTip("按这个键：书页换成 Anki 卡片（还是这本书的字体/字号/行距/页边距）。\n"
                                       "再按一次退出，书回到原来那一页，阅读进度一个字节都不变。")
        layout.addRow("背诵热键:", self.anki_key_input)

        deck_row = QWidget()
        deck_layout = QHBoxLayout(deck_row)
        deck_layout.setContentsMargins(0, 0, 0, 0)
        self.anki_deck_input = QLineEdit(self.config.get("anki_deck", ""))
        self.anki_deck_input.setPlaceholderText("牌组名，例如 背诵")
        self.anki_deck_input.setToolTip("要背哪个牌组（子牌组会自动包含在内）。\n"
                                        "点「列出牌组…」可以从正在运行的 Anki 里读出来（带今日待复习张数）。")
        btn_pick_deck = QPushButton("列出牌组…")
        btn_pick_deck.clicked.connect(self.pick_anki_deck)
        deck_layout.addWidget(self.anki_deck_input)
        deck_layout.addWidget(btn_pick_deck)
        layout.addRow("牌组:", deck_row)

        self.anki_swallow_check = QCheckBox("吞掉空格/回车（评分键 1-4 一定吞）")
        self.anki_swallow_check.setChecked(bool(self.config.get("anki_swallow_keys", True)))
        self.anki_swallow_check.setToolTip("背诵时这两个键归卡片（翻面/继续）。\n"
                                           "关掉后空格/回车还给系统，正常打字不受影响——\n"
                                           "翻面改用鼠标点一下卡片（和 Anki 一样），评分仍用 1/2/3/4。\n"
                                           "不想被打字打扰时，随时 Ctrl+D 把窗口收起来即可（收起来就一个键都不吞）。")
        layout.addRow(self.anki_swallow_check)

        self.anki_conceal_check = QCheckBox("背诵时把 Anki 自己的窗口藏起来")
        self.anki_conceal_check.setChecked(bool(self.config.get("anki_conceal", True)))
        self.anki_conceal_check.setToolTip("Anki 自己的界面会从屏幕、Alt+Tab 和任务栏缩略图里消失，\n"
                                           "答题全在这个窗口里完成；退出背诵时它会回到原处（不激活）。\n"
                                           "注意：藏起来≠隐身，别把两个窗口摆在同一块屏幕上。")
        layout.addRow(self.anki_conceal_check)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self.anki_api_key_input = QLineEdit(self.config.get("anki_api_key", ""))
        self.anki_api_key_input.setPlaceholderText("留空 = AnkiConnect 没设 apiKey")
        self.anki_api_key_input.setEchoMode(QLineEdit.Password)
        self.anki_api_key_input.setToolTip("AnkiConnect 配置里若设了 apiKey，填在这里；没设就留空。")
        btn_test_anki = QPushButton("测试连接")
        btn_test_anki.clicked.connect(self.test_anki_connection)
        key_layout.addWidget(self.anki_api_key_input)
        key_layout.addWidget(btn_test_anki)
        layout.addRow("AnkiConnect Key:", key_row)

        btn_save = QPushButton("💾 保存并应用")
        btn_save.clicked.connect(self.accept)
        layout.addRow(btn_save)

        self.on_auto_mode_toggled(self.check_auto_mode.isChecked())
        self.on_antishot_toggled(self.check_antishot.isChecked())
        self.setLayout(layout)

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
        self.config["first_line_indent"] = self.indent_spin.value()
        self.config["paragraph_indent"] = self.para_indent_check.isChecked()
        if self.main_window:
            self.main_window.apply_style()
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
            info = stats.get(name) or {}
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
        self.config["first_line_indent"] = self.indent_spin.value()
        self.config["paragraph_indent"] = self.para_indent_check.isChecked()
        self.config["boss_key"] = self.boss_key_input.text().strip()
        self.config["peek_key"] = self.peek_key_input.text().strip()
        self.config["safe_key"] = self.safe_key_input.text().strip()
        self.config["safe_doc_path"] = self.safe_doc_input.text().strip()
        self.config["anki_key"] = self.anki_key_input.text().strip() or "ctrl+alt+a"
        self.config["anki_deck"] = self.anki_deck_input.text().strip()
        self.config["anki_swallow_keys"] = self.anki_swallow_check.isChecked()
        self.config["anki_conceal"] = self.anki_conceal_check.isChecked()
        self.config["anki_api_key"] = self.anki_api_key_input.text().strip()
        self.config["text_color"] = self.temp_text_color
        self.config["bg_color"] = self.temp_bg_color
        self.config["ghost_mode"] = self.check_ghost_mode.isChecked()
        self.config["auto_mode"] = self.check_auto_mode.isChecked()
        self.config["antishot_mode"] = self.check_antishot.isChecked()
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
    hotkey_signal = pyqtSignal()
    peek_hotkey_signal = pyqtSignal()
    safe_hotkey_signal = pyqtSignal()
    review_hotkey_signal = pyqtSignal()
    # 偷瞄模式的钩子回调只发信号（QueuedConnection），翻页和退出都回到主线程做
    peek_wheel_signal = pyqtSignal(int)
    peek_exit_signal = pyqtSignal(str)
    bookshelf_updated_signal = pyqtSignal(list)
    # 背诵模式的按键也来自钩子线程（那里不能碰界面），一律发信号回主线程处理
    review_key_signal = pyqtSignal(str)

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
        self.hotkey_signal.connect(self.toggle_window)
        self.peek_hotkey_signal.connect(self.toggle_peek)
        self.safe_hotkey_signal.connect(self.toggle_safe_mode)
        self.review_hotkey_signal.connect(self.toggle_review_mode)
        self.peek_wheel_signal.connect(self.on_peek_wheel, Qt.QueuedConnection)
        self.peek_exit_signal.connect(self.on_peek_exit, Qt.QueuedConnection)
        self.review_key_signal.connect(self.on_review_key, Qt.QueuedConnection)
        self.bookshelf_updated_signal.connect(self.on_bookshelf_updated)
        # 最后一道保险：任何退出路径（托盘退出、右键退出、关窗、异常退出）都要停掉 Anki
        # 后台线程。QThread 还在跑就被析构，PyQt 会直接崩，而且 Anki 可能停在"藏起来"的状态。
        try:
            QApplication.instance().aboutToQuit.connect(self.stop_anki_worker)
        except Exception:
            pass

        self.refresh_hotkeys()
        self.register_session_notification()   # 锁屏解锁后靠它主动重挂热键

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

    def apply_page_margins(self):
        """页边距（四边）：左右和顶部是固定的视口留白，底部按"本页最后一行完整"再收一次。

        分成两层是有原因的：
          - 固定框（viewportMargins 的上下左右）：滚动吃不掉，所以偷瞄用滚轮平滑滚动时
            顶部那条留白照样在（文档留白会被内容滚过去，看着就是"顶部没有页边距"）；
          - 底部再按行位置收边：正文是按几何切页的，切点落在哪一行就显示到哪，
            压在窗口下沿的那一行会被切掉半行，所以要退到最后一个完整行的下沿。
        四边量到的是同一个东西——正文墨迹：顶部/左右是固定留白，底部再多补回最后一行行盒
        里那几像素行距（见 page_cut_line），于是"看得见的底部留白 ≥ 设定值"且最小就是设定值。"""
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
        """换文本的唯一入口：换完立刻套同一套排版，否则翻页会按旧排版测量。"""
        self.text_edit.setPlainText(text)
        self.apply_text_format()

    # --- 本地分页渲染算法 (锚点核心) ---
    def render_local_page(self):
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

        return next_pos_in_buffer

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
                if current_val >= max_val - 5:
                    self.next_chapter()
                else:
                    scrollbar.setValue(min(target_val, max_val))
            else:
                if current_val <= min_val + 5:
                    self.prev_chapter()
                else:
                    scrollbar.setValue(max(target_val, min_val))

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
        try:
            keyboard.unhook_all()
            keyboard.add_hotkey(hotkey_str, self.on_global_hotkey_triggered)
            # 注意：unhook_all 只拆 keyboard 库自己的钩子；偷瞄/背诵用的 ctypes 钩子是独立管理的，
            # 所以看门狗每 90 秒的例行重挂碰不到它——背诵吞键不需要在重挂后补装。
            keyboard.add_hotkey(peek_key_str, self.on_peek_hotkey_triggered)
            taken = {hotkey_str.lower(), peek_key_str.lower()}
            # 安全文档热键：和上面两个撞车的话只留先注册的那个，免得一次按键触发两个动作
            if safe_key_str and safe_key_str.lower() not in taken:
                keyboard.add_hotkey(safe_key_str, self.on_safe_hotkey_triggered)
                taken.add(safe_key_str.lower())
            elif safe_key_str:
                log_event(f"安全文档热键与老板键/偷瞄键重复，已跳过: {safe_key_str}")
            # 隐形背诵热键：同样避免一次按键触发两个动作
            if review_key_str and review_key_str.lower() not in taken:
                keyboard.add_hotkey(review_key_str, self.on_review_hotkey_triggered)
            elif review_key_str:
                log_event(f"背诵热键与已有热键重复，已跳过: {review_key_str}")
        except Exception as e:
            log_event(f"全局热键注册失败: {e}")
        try:
            if self.local_shortcut:
                self.local_shortcut.setKey(QKeySequence())
                self.local_shortcut = None
            self.local_shortcut = QShortcut(QKeySequence(hotkey_str), self)
            self.local_shortcut.activated.connect(self.toggle_window)
        except:
            pass
        try:
            if self.peek_shortcut:
                self.peek_shortcut.setKey(QKeySequence())
                self.peek_shortcut = None
            self.peek_shortcut = QShortcut(QKeySequence(peek_key_str), self)
            self.peek_shortcut.activated.connect(self.toggle_peek)
        except:
            pass
        try:
            if self.safe_shortcut:
                self.safe_shortcut.setKey(QKeySequence())
                self.safe_shortcut = None
            if safe_key_str:
                self.safe_shortcut = QShortcut(QKeySequence(safe_key_str), self)
                self.safe_shortcut.activated.connect(self.toggle_safe_mode)
        except:
            pass
        try:
            if self.review_shortcut:
                self.review_shortcut.setKey(QKeySequence())
                self.review_shortcut = None
            if review_key_str:
                self.review_shortcut = QShortcut(QKeySequence(review_key_str), self)
                self.review_shortcut.activated.connect(self.toggle_review_mode)
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

    def on_global_hotkey_triggered(self):
        # 这三个回调跑在 keyboard 库的分发线程上，那里没有兜底 try：回调抛异常（比如日志
        # 写盘失败）会把分发线程直接打死，此后线程活着、listening 仍是 True，热键却一个都不响。
        # 所以自己兜住，绝不把异常抛回库里。
        try:
            log_event(f"老板键触发 boss_key={self.config.get('boss_key')}")
            self.hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"老板键回调异常: {e}")
            except Exception:
                pass

    def on_peek_hotkey_triggered(self):
        try:
            self.peek_hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"偷瞄键回调异常: {e}")
            except Exception:
                pass

    def on_safe_hotkey_triggered(self):
        try:
            log_event("安全文档热键触发")
            self.safe_hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"安全文档键回调异常: {e}")
            except Exception:
                pass

    def on_review_hotkey_triggered(self):
        try:
            log_event("隐形背诵热键触发")
            self.review_hotkey_signal.emit()
        except Exception as e:
            try:
                log_event(f"背诵键回调异常: {e}")
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
            if self.review_mode:
                self.install_review_hook()      # 钩子万一被系统摘掉，这里补回来
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
        # 鼠标事件其实都发给 viewport（上次滚轮那个坑），所以划选/单击必须装在 viewport 上
        self.text_edit.viewport().installEventFilter(self)
        self.text_edit.viewport().setMouseTracking(True)
        # 悬停事件是兜住光标用的：真实鼠标移动时它的处理顺序在最后
        self.text_edit.viewport().setAttribute(Qt.WA_Hover, True)

        self.content_layout.addWidget(self.text_edit)

        # 背诵专用页脚：一行"学习计划 + 今日/本次 + 四个评分按钮（各自带下次复习间隔）"。
        # 平时整条藏着；安全文档盖上来时也藏着——周报底下挂一排评分按钮，一眼就露馅。
        self.review_bar = QWidget()
        # 竖向 Maximum：页脚永远只占它需要的那一点高度。默认策略下 QVBoxLayout 会把
        # 多余空间也分给它（实测占了半个窗口高），正文区就被压掉一半。
        self.review_bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        bar_layout = QHBoxLayout(self.review_bar)
        bar_layout.setContentsMargins(6, 2, 6, 4)
        bar_layout.setSpacing(0)
        self.review_plan_label = QLabel("")
        self.review_plan_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        bar_layout.addWidget(self.review_plan_label)
        self.review_buttons = []
        for _i in range(4):
            _btn = QPushButton("")
            _btn.setFlat(True)
            _btn.setFocusPolicy(Qt.NoFocus)
            _btn.setCursor(Qt.ArrowCursor)
            _btn.clicked.connect(lambda _=False, n=_i + 1: self.review_grade_button(n))
            bar_layout.addWidget(_btn)
            self.review_buttons.append(_btn)
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
                # 偷瞄时双击正文 = 换成安全文档（窗口继续亮着，内容无害）
                self.enter_safe_mode()
                return True
            if event.type() == QEvent.MouseButtonPress and self.on_viewport_press(event):
                return True
            if event.type() == QEvent.MouseMove and self.on_viewport_move(event):
                return True
            if event.type() == QEvent.MouseButtonRelease and self.on_viewport_release(event):
                return True
        if source == self.text_edit and event.type() == QEvent.Wheel:
            # 【关键保护】如果既没选书，也不是本地模式，直接拦截滚轮不处理
            if not self.is_local_mode and not self.current_book:
                return True
            if self.review_mode:
                # 背诵时滚轮滚的是卡片（交给 Qt 默认逐行滚动），不翻页、不碰书的进度
                return False

            delta = event.angleDelta().y()
            if self.is_local_mode:
                if delta < 0:
                    self.scroll_page(1)
                elif delta > 0:
                    self.scroll_page(-1)
                return True
            else:
                scrollbar = self.text_edit.verticalScrollBar()
                if delta < 0:
                    if scrollbar.value() >= scrollbar.maximum() - 2:
                        self.next_chapter()
                        return True
                elif delta > 0:
                    if scrollbar.value() <= scrollbar.minimum() + 2:
                        self.prev_chapter()
                        return True
        return super().eventFilter(source, event)

    def _point_on_desktop(self, x, y):
        """坐标是否还落在某块屏幕上（虚拟桌面并集内），防止换了显示器窗口飞出屏。"""
        union = QRect()
        for scr in QApplication.instance().screens():
            union = union.united(scr.geometry())
        return not union.isNull() and union.contains(x, y)

    def save_window_pos(self):
        """记住窗口当前位置，下次启动原位出现。"""
        try:
            self.config["win_x"] = self.x()
            self.config["win_y"] = self.y()
            self.save_config()
        except Exception:
            pass

    def ensure_on_screen(self):
        """召唤/启动后确保窗口真的在屏幕上。Qt 缓存过期屏幕布局（拔过显示器的机器）
        会把窗口安排到不存在的屏幕坐标里，看起来就是"唤不出来"。用 Win32 真实布局校验。"""
        try:
            hwnd = int(self.winId())
            if window_on_real_screen(hwnd):
                return
            if force_window_to_primary(hwnd):
                log_event("窗口不在任何真实屏幕上，已强制挪回主屏")
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
        tray_menu.addAction("显示/隐藏（老板键）").triggered.connect(self.toggle_window)
        tray_menu.addAction("隐形背诵（Anki 卡片）").triggered.connect(self.toggle_review_mode)
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
            self.toggle_window()

    def toggle_window(self):
        current_time = time.time()
        if current_time - self.last_toggle_time < 0.3:
            return
        self.last_toggle_time = current_time

        if self.isVisible():
            log_event("老板键 -> 隐藏窗口")
            self.dismiss_action_panel(cancelled=True)   # 连同待确认的划选一起作废
            self.sync_progress_async()
            if self.is_local_mode and not self._book_veiled():
                # 收起来 = "先看到这儿"：记视口顶那一行，而不是本页页首（页内滚过会差一屏）
                self.config["last_local_pos"] = self.current_top_offset()
            self.save_window_pos()
            self.hide()
        else:
            log_event("老板键 -> 显示窗口")
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
        now = time.time()
        if now - self.peek_last_toggle < 0.3:
            return
        self.peek_last_toggle = now
        if self.review_mode:
            # 背诵中按偷瞄键：按设计先把背诵退掉（书页回来），再照常进偷瞄
            self.exit_review_mode(reason="peek")
        if self.safe_mode:
            # 安全文档下按偷瞄键也一样：先回到书，再浮现书页（偷瞄的永远是书）
            self.exit_safe_mode()
        if self.peek_mode:
            self.exit_peek("peek_key")
            return
        if now - self.peek_last_exit < 0.35:
            return  # 刚退出的那一下按键余波，别当成新指令再进去
        self.enter_peek()

    def enter_peek(self):
        """浮现但不抢焦点，挂全局滚轮。挂不上钩子就不进——半吊子的偷瞄只会让人困惑。"""
        if self.peek_mode:
            return
        if self.safe_mode:
            # 从安全文档进偷瞄：先把真书放回来再看，否则偷瞄到的还是那份假页面
            self.exit_safe_mode()
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

    def exit_peek(self, reason=""):
        """退出偷瞄 = **把窗口收起来**。

        偷瞄是"临时看一眼"，看完就该让它从屏幕上消失——包括从安全文档进来的那次：
        退出时不再退回安全文档那副假页面（那样窗口还亮着，等于没走），而是直接隐藏。
        唯一例外是"交给安全文档"（双击正文 / 按 Ctrl+Alt+D 切过去），那时窗口继续亮着，
        只是内容换成安全文档。要再叫出来：Ctrl+E 继续偷瞄，或老板键 / 托盘图标显示窗口。"""
        if not self.peek_mode:
            return
        self.peek_mode = False
        self.peek_last_exit = time.time()
        self.text_edit.set_pinned_cursor(Qt.ArrowCursor)
        self.peek_input_hook.uninstall()  # 先卸钩，后面任何一步出错都不能泄漏钩子
        self.apply_style()                # 先恢复正常样式，免得偷瞄那套样式留到下次露脸
        if self.safe_mode:
            self.exit_safe_mode()         # 从安全文档进来的：真书放回来（窗口马上要藏起来）
        if reason == "safe_mode":
            return                        # 交给安全文档：窗口继续亮着，内容换成安全文档
        self.sync_progress_async()
        self.hide()
        log_event(f"偷瞄退出（{reason or '未说明'}）→ 窗口已隐藏")

    def on_peek_wheel(self, delta):
        """偷瞄模式的滚轮 = 窗口内滚动的手感：一格滚几行（事件交给 QTextEdit 默认处理，
        实测一格约 60px ≈ 3 行），滚到 5000 字缓冲区边沿后自动衔接前后页，不跳字不空页。"""
        if not self.is_local_mode:
            # 网络模式与窗口内行为一致：只滚缓冲区
            QApplication.sendEvent(self.text_edit.viewport(), self._synth_wheel_event(delta))
            return
        sb = self.text_edit.verticalScrollBar()
        QApplication.sendEvent(self.text_edit.viewport(), self._synth_wheel_event(delta))
        if delta < 0 and sb.maximum() > 0 and sb.value() >= sb.maximum() - 2:
            # 缓冲区到底：下一页从"当前视口顶部"衔接，已看的字不重不漏
            cursor = self.text_edit.cursorForPosition(QPoint(0, 0))
            new_start = min(self.local_start_index + cursor.position(),
                            max(0, len(self.local_full_text) - 1))
            if new_start > self.local_start_index:
                self.local_page_history.append(self.local_start_index)
                self.local_start_index = new_start
                self.render_local_page()
        elif delta > 0 and sb.value() <= 2:
            # 缓冲区到顶：渲染更早的 5000 字并停在其底部，从这里继续往上滚
            new_start = max(0, self.local_start_index - 5000)
            if new_start < self.local_start_index:
                self.local_start_index = new_start
                self.render_local_page()
                self.text_edit.verticalScrollBar().setValue(
                    self.text_edit.verticalScrollBar().maximum())

    def _synth_wheel_event(self, delta):
        """造一个和真实滚轮等价的事件交给 viewport，走 Qt 默认的逐行滚动路径。"""
        vp = self.text_edit.viewport()
        return QWheelEvent(QPointF(vp.rect().center()), QPointF(QCursor.pos()),
                           QPoint(0, 0), QPoint(0, delta),
                           Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)

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

    def restore_book_state(self, snap, write_progress=True):
        """按快照把书页放回来。

        `write_progress=False` 时一个字节都不许写：隐形背诵退出时正文区还是卡片，
        按几何算出来的"锚点"属于卡片，写进 last_local_pos 就把真书的阅读进度改坏了
        （安全文档那条路要写，因为它恢复的就是书自己的位置）。"""
        if not snap:
            return
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

    def enter_safe_mode(self):
        """切到安全文档：排版沿用当前设置（同一条渲染管线），窗口位置大小不动，
        划线高亮全部藏起来，书的状态先存快照——退出后原位恢复。"""
        if self.safe_mode:
            return
        if self.review_mode:
            # 从背诵里切过来：**不能拍书的快照**——此刻正文区是卡片，拍下来的是卡片的坐标，
            # 退出安全文档时会拿它去恢复书页，把书的位置弄坏。只记一个"我是从卡片来的"。
            self.safe_snapshot = {"from_review": True}
            self.dismiss_action_panel(cancelled=True)
            self.exit_peek("safe_mode")
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
        self.exit_peek("safe_mode")
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
        return bool(self.safe_mode or self.review_mode)

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
            except Exception:
                pass
        self.stop_anki_worker()


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
        if self.peek_mode:
            # 从偷瞄进来：只收起偷瞄（不隐藏窗口，马上要显示卡片）
            self.peek_mode = False
            self.peek_input_hook.uninstall()
            self.text_edit.set_pinned_cursor(Qt.ArrowCursor)
        if self.safe_mode:
            self.exit_safe_mode()              # 从安全文档进来：先把真书放回来，再拍快照
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
        self.install_review_hook()         # 吞那 9 个复习键（只在背诵中 + 窗口可见时吞）
        self.review_timer.start()
        if self.config.get("anki_conceal", True):
            try:
                self.anki_client().conceal_anki()   # 纯 win32 调用，不排 Anki 的队
            except Exception:
                pass
        self.apply_counts(r.get("stats"), r.get("reviewed"))
        self.review_load_card(r.get("card"))
        log_event(f"隐形背诵：已进入（牌组 {deck}）")

    def apply_review_bar_style(self):
        """页脚的字号/颜色跟着阅读设置走，只是小一号、淡一点——像文档底下的一行小字。"""
        size = max(8, int(self.config.get("font_size", 14)) - 3)
        color = QColor(200, 200, 200)
        try:
            base = QColor(str(self.config.get("text_color") or ""))
            if base.isValid():
                color = base
        except Exception:
            pass
        c = f"rgba({color.red()},{color.green()},{color.blue()},155)"
        dim = f"rgba({color.red()},{color.green()},{color.blue()},75)"
        self.review_bar.setStyleSheet(
            f"QLabel {{ color: {c}; font-size: {size}px; background: transparent; }}"
            f"QPushButton {{ color: {c}; font-size: {size}px; background: transparent;"
            f" border: none; padding: 3px 6px; }}"
            f"QPushButton:hover {{ background: rgba(255,255,255,0.12); border-radius: 3px; }}"
            f"QPushButton:disabled {{ color: {dim}; }}")

    def update_review_bar(self):
        """页脚内容：学习计划（新/学/复）+ 今日与本次张数 + 四个评分按钮。

        按钮文字带 Anki 给的 `nextReviews`（"重来 <10 分 / 良好 3 天"），这就是按下去
        **会对这张卡产生的结果**；翻面前按钮是灰的（Anki 也不让你没看答案就评分），
        翻面后按 `buttons` 逐个启用——学习步/FSRS 下按钮可能只有 2 个，多的藏起来。
        """
        if not hasattr(self, "review_bar"):
            return
        visible = bool(self.review_mode and not self.safe_mode)
        self.review_bar.setVisible(visible)
        if not visible:
            return
        self.apply_review_bar_style()
        s = self.review_session or {}
        card = s.get("card") or {}
        buttons = list(card.get("buttons") or [1, 2, 3, 4])
        times = list(card.get("nextReviews") or [])
        labels = REVIEW_BUTTON_LABELS.get(len(buttons))
        if labels is None:
            labels = ["重来", "困难", "良好", "容易"][:len(buttons)]
        for i, btn in enumerate(self.review_buttons):
            if i < len(buttons):
                name = labels[i] if i < len(labels) else str(buttons[i])
                when = times[i] if i < len(times) else ""
                btn.setText(f"{buttons[i]} {name}" + (f" {when}" if when else ""))
                btn.setVisible(True)
                btn.setEnabled(bool(s.get("shown")))
            else:
                btn.setText("")
                btn.setEnabled(False)
                btn.setVisible(False)
        plan = ""
        if s.get("new_count") is not None:
            plan = f"新 {s.get('new_count')} · 学 {s.get('learn_count')} · 复 {s.get('review_count')}"
        tail = []
        if s.get("reviewed_today") is not None:
            tail.append(f"今日已 {s['reviewed_today']}")
        if s.get("counted"):
            tail.append(f"本次 {s['counted']}")
        if not card:
            hint = "今天清完了"
        elif s.get("shown"):
            hint = "评分"
        else:
            hint = "空格 / 点一下卡片 翻面"
        self.review_plan_label.setText("　".join(x for x in [plan, " · ".join(tail), hint] if x))

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
        self.apply_page_margins()
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
        self.update_review_bar()   # 评分按钮立刻收掉，别留在书页上
        if self.review_client is not None:
            client = self.review_client
            if self.anki_worker is not None:
                # 也走后台：退出的那 60ms 不该卡住界面。退出程序时 quit_app 会再同步兜一次，
                # 保证 Anki 窗口绝对不会留在"藏起来"的状态。
                self.anki_call(lambda: (client.leave_review(), client.reveal_anki()), key="leave")
        if restore and snap:
            # 安全文档正盖在卡片上（而且就是从背诵切过来的）：一起收掉，直接回到书。
            # 否则会走进"从安全文档退出→回到卡片"的分支，而背诵这时已经退出了，留下一屏卡片残影。
            if self.safe_mode and (self.safe_snapshot or {}).get("from_review"):
                self.safe_mode = False
                self.safe_snapshot = None
            self.restore_book_state(snap, write_progress=False)
        self.review_timer.stop()
        if not self.peek_mode:
            # 卸掉吞键钩子（钩子泄漏的症状是全系统输入发卡）；偷瞄模式下钩子还要用，留着
            self.peek_input_hook.uninstall()
        self.update_tray_tooltip()
        log_event(f"隐形背诵：已退出（{reason or '未说明'}，本次背了 {session.get('counted', 0)} 张）")

    # --- 复习键 ---

    def review_swallow_all(self):
        """是否连空格/回车也吞（设置项；关掉就只吞 1-4/U/R/Esc，正常打字不受影响）。"""
        return bool(self.config.get("anki_swallow_keys", True))

    def review_keys_active(self):
        """吞键的前提：背诵中 + 窗口可见 + 没最小化 + **没被安全文档盖住**。

        隐藏（Ctrl+D）时 `isVisible()` 为 False，键立刻全部放行——不必重挂钩子；
        最小化的窗口 `isVisible()` 仍为 True，所以要单独判一次，否则会把人家的空格吃掉；
        安全文档模式下更要一个键都不吞：那会儿屏幕上是"工作周报"，敲不出空格立刻露馅。
        """
        return bool(self.review_mode and not self.safe_mode
                    and self.isVisible() and not self.isMinimized())

    def install_review_hook(self):
        """背诵也要用到那个 ctypes 钩子（和偷瞄共用一套，互不干扰）。"""
        try:
            return self.peek_input_hook.install(self.config.get("peek_key", "ctrl+e"))
        except Exception as e:
            log_event(f"背诵吞键钩子安装失败（不吞键也能用）: {e}")
            return False

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

        def job():
            client.show_answer()
            client.play_audio()          # 翻面自动放这一面（Anki 自己的语义）
            return client.current_card()

        def done(card):
            s2 = self.review_session
            if not s2 or s2.get("card_id") != prev_id:
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

        def job():
            out = {"card": client.current_card()}
            if want_counts:
                out["stats"] = client.deck_stats([deck])
                out["reviewed"] = client.reviewed_today()
            return out

        self.anki_call(job, on_ok=lambda r, f=force: self.finish_poll(r, f),
                       on_err=self.fail_poll, key="poll")

    def finish_poll(self, r, force=False):
        s = self.review_session
        if not s:
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

        def job():
            client.undo()
            return (client.current_card(), client.deck_stats([deck]) if deck else None)

        def done(res):
            card, stats = res
            s = self.review_session
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
            self.content_frame.set_draw_corners(True)
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
        if self.review_mode and not self.safe_mode:
            self.render_card()     # 背诵中改字号/字体：只重渲当前卡（一个字节的进度都不碰）
        elif self.is_local_mode:
            self.repage_to(anchor)
        self.update_review_bar()   # 页脚（学习计划 + 评分按钮）跟着字号/颜色与模式走

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

                # 【核心逻辑】调整大小时基于锚点重绘
                if self.is_local_mode:
                    self.repage_to(anchor)

            elif self.is_moving:
                delta = QPoint(event.globalPos() - self.oldPos)
                self.move(self.x() + delta.x(), self.y() + delta.y())
                self.oldPos = event.globalPos()

            if self.config.get("auto_mode"):
                self.adjust_color_to_background()

    def mouseReleaseEvent(self, event):
        self.is_resizing = False
        self.is_moving = False
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
        return False

    def on_viewport_release(self, event):
        if self.filter_drag_origin is not None:
            self.filter_drag_origin = None
            self.setCursor(Qt.ArrowCursor)
            self.text_edit.set_pinned_cursor(Qt.ArrowCursor)   # 拖动结束，正文区回箭头
            self.save_window_pos()
            return True
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
        """滚轮：视为不确认，但滚轮本身照样翻页（手动把这一下补回去）。"""
        self.dismiss_action_panel(cancelled=True)
        try:
            QApplication.sendEvent(self.text_edit.viewport(), self._synth_wheel_event(delta))
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

    def build_context_menu(self):
        """右键菜单。安全文档模式下只给中性项：菜单里的"打开本地书籍/导出划线笔记"这类字眼
        本身就是破绽，凑过来看屏幕的人一眼就懂了。"""
        cmenu = QMenu(self)
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
        if self.review_mode:
            # 背诵时正文是卡片：方向键/空格翻的是卡片本身，不翻书页
            sb = self.text_edit.verticalScrollBar()
            page = max(1, self.text_edit.viewport().height())
            if key in [Qt.Key_Right, Qt.Key_Down, Qt.Key_Space, Qt.Key_PageDown]:
                sb.setValue(min(sb.maximum(), sb.value() + page))
            elif key in [Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp]:
                sb.setValue(max(sb.minimum(), sb.value() - page))
            return
        if key in [Qt.Key_Right, Qt.Key_Down, Qt.Key_Space, Qt.Key_PageDown]:
            self.scroll_page(1)
        elif key in [Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp]:
            self.scroll_page(-1)

    def closeEvent(self, event):
        self.exit_peek("close")  # 窗口关了必须先卸全局钩子，再走保存
        if self.review_mode:
            self.exit_review_mode(reason="close")   # 关窗口＝整套收起（只想藏卡片用 Ctrl+D）
        self.shutdown_anki()     # X 关窗是最常用的退出方式：这里也必须把后台线程停干净
        self.dismiss_action_panel(cancelled=True)
        self.sync_progress_async()
        self.save_window_pos()
        if self.is_local_mode and not self._book_veiled():
            self.config["last_local_pos"] = self.current_top_offset()   # 视口顶那一行，不是页首
            self.save_config()
        super().closeEvent(event)

    def quit_app(self):
        # 退出前强制保存本地进度（安全文档/背诵模式下不存：那些坐标不是书的进度）
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