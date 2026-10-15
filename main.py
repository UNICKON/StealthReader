import sys
import os

# 自带依赖目录（PyQt5 / keyboard 都解包在 libs/）：不依赖启动器设置 PYTHONPATH，
# 任何方式拉起本文件都能活。必须放在 import keyboard 之前。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs"))

import requests
import json
import re
import posixpath
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
                             QListWidgetItem, QLabel, QFontComboBox, QSizePolicy, QFileDialog)
from PyQt5.QtCore import Qt, QPoint, QRect, QPointF, pyqtSignal, QObject, QThread, QTimer, QEvent
from PyQt5.QtGui import (QFont, QColor, QCursor, QKeySequence, QPainter, QPen,
                         QFontMetrics, QTextCursor, QTextBlockFormat, QWheelEvent)

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
PAGE_MARGIN_MIN = 0
PAGE_MARGIN_MAX = 60
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
    "page_margin": 0,
    "first_line_indent": FIRST_LINE_INDENT_DEFAULT,
    "paragraph_indent": True,
    # 标题永远不缩进；这个键只作为 txt 猜错标题时的手动开关，不在界面上
    "heading_no_indent": True,
    "peek_key": "ctrl+e",
    "text_color": "rgba(200, 200, 200, 255)",
    "bg_color": "rgba(30, 30, 30, 200)",
    "boss_key": "Esc",
    "ghost_mode": False,
    "auto_mode": False,
    "antishot_mode": False,
    "window_width": 400,
    "window_height": 300,
    "last_local_file": "",
    "last_local_pos": 0
}

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
        self.heading_lines = set()
        self._buf = []
        self._skip = 0
        self._heading_depth = 0

    def _flush(self):
        line = re.sub(r"[ \t\f\v\u00a0\u3000]+", " ", "".join(self._buf)).strip()
        self._buf = []
        if line:
            if self._heading_depth:
                self.heading_lines.add(len(self.lines))
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
                self._heading_depth += 1

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
            if tag in EPUB_HEADING_TAGS and self._heading_depth:
                self._heading_depth -= 1

    def handle_data(self, data):
        if self._skip:
            return
        for i, segment in enumerate(data.split("\n")):
            if i:
                self._flush()
            if segment:
                self._buf.append(segment)

    def finish(self):
        """返回 (纯文本, {锚点: 字符偏移}, {标题行的字符偏移})。"""
        self._flush()
        text = "\n".join(self.lines)
        offsets = [0]
        for line in self.lines:
            offsets.append(offsets[-1] + len(line) + 1)
        anchors = {key: offsets[min(idx, len(self.lines))]
                   for key, idx in self.ids.items()}
        headings = {offsets[idx] for idx in self.heading_lines}
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
        return "", {}, set()
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
    """按文档顺序返回 [(zip 内路径, 锚点, 标题)]：优先 epub3 的 nav，其次 epub2 的 ncx。"""
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
                for a in nav.iter():
                    if _epub_local_name(a.tag) != "a":
                        continue
                    target, fragment = resolve(a.get("href"))
                    entries.append((target, fragment, "".join(a.itertext())))
        elif ncx_href:
            root = read_xml(ncx_href)
            for point in root.iter():
                if _epub_local_name(point.tag) != "navPoint":
                    continue
                href, label = None, ""
                for child in point.iter():
                    name = _epub_local_name(child.tag)
                    if name == "content" and child.get("src"):
                        href = child.get("src")
                    elif name == "text" and not label:
                        label = "".join(child.itertext())
                target, fragment = resolve(href)
                entries.append((target, fragment, label))
    except Exception:
        pass
    return [(t, f, l) for t, f, l in entries if l and l.strip()]


def _load_epub(path):
    """解析 epub，返回 (全文, [(章节标题, 起始字符偏移), ...], {标题行偏移})。"""
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

        has_anchors = any(fragment for _, fragment, _ in toc)

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
            for target, fragment, label in toc:
                if target not in file_start:
                    continue
                offset = file_start[target] + doc_anchors.get(target, {}).get(fragment, 0)
                if offset in used_offsets:
                    continue
                used_offsets.add(offset)
                chapters.append((_epub_clean_label(label), offset))
            chapters.sort(key=lambda item: item[1])

            # 标题行 = 原文里的 h1~h6，加上目录标题所在的那一行
            headings = {file_start[target] + offset
                        for target, offsets in file_headings.items()
                        for offset in offsets}
            headings.update(offset for _label, offset in chapters)
            return "".join(blocks), chapters, headings

        # 目录缺失或只到文件级：一个文件一章，需要时补一行标题
        titles = {}
        for target, _fragment, label in toc:
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

            chapters.append((title, cursor))
            headings.add(cursor)  # 标题行本身
            shift = len(title) + 2 if prepended else 0
            headings.update(cursor + shift + offset for offset in doc_headings)
            blocks.append(piece)
            cursor += len(piece)

        return "".join(blocks), chapters, headings


def load_epub_with_headings(path):
    """解析 epub，额外返回标题行偏移（首行缩进要用它来跳过标题）。"""
    return _load_epub(path)


def load_epub_text(path):
    """解析 epub，返回 (全文, [(章节标题, 起始字符偏移), ...])。"""
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


# ================= 轻量事件日志（排查老板键之类"偶尔失灵"的问题）=================
def log_event(msg):
    """追加一行到 main.py 旁边的 stealthreader.log，失败静默（日志不能反过来影响功能）。"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stealthreader.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ================= 低级输入钩子（偷瞄模式：全局滚轮翻页） =================
# 钩子回调由 Windows 在毫秒级内强拆超时者，所以这里只做判断和转发，
# 真正的翻页通过 Qt 队列信号回到主线程慢慢做。任何退出路径都必须调用 uninstall，
# 否则泄漏的钩子会让全系统滚轮发卡。
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_MOUSEWHEEL = 0x020A
WM_LBUTTONDOWN = 0x0201
WM_MBUTTONDOWN = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_XBUTTONDOWN = 0x020B
MOUSE_DOWN_MESSAGES = {WM_LBUTTONDOWN, WM_MBUTTONDOWN, WM_RBUTTONDOWN, WM_XBUTTONDOWN}

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


_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)  # WPARAM/LPARAM
_user32 = ctypes.windll.user32
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
_user32.CallNextHookEx.restype = _LRESULT


class GlobalInputHook:
    """偷瞄模式的全局输入钩子：滚轮翻页（吞掉，底下应用不滚），按键/点击退出（放行）。

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
                if message in MOUSE_DOWN_MESSAGES:
                    self.owner.peek_exit_signal.emit("click")
                    return 0  # 点击放行，用户多半在恢复干活
        except Exception:
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _on_keyboard(self, n_code, w_param, l_param):
        try:
            if n_code >= 0 and self.owner.peek_mode and int(w_param) in (WM_KEYDOWN, WM_SYSKEYDOWN):
                info = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
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
class LocalTocSelector(QDialog):
    def __init__(self, chapters, current_offset, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"📑 目录 (共 {len(chapters)} 章)")
        self.resize(420, 600)
        self.setStyleSheet(DARK_STYLESHEET)
        self.chapters = chapters
        self.selected_offset = None

        layout = QVBoxLayout()
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        layout.addWidget(self.list_widget)

        hint = QLabel("双击章节跳转（当前章节已选中）")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)
        self.setLayout(layout)

        for title, offset in chapters:
            item = QListWidgetItem(str(title))
            item.setData(Qt.UserRole, offset)
            self.list_widget.addItem(item)

        current = 0
        for i, (_, offset) in enumerate(chapters):
            if offset <= current_offset:
                current = i
        item = self.list_widget.item(current)
        if item:
            item.setSelected(True)
            self.list_widget.scrollToItem(item, QListWidget.PositionAtCenter)

    def on_item_double_clicked(self, item):
        self.selected_offset = item.data(Qt.UserRole)
        self.accept()


# ================= 设置窗口 =================
class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.main_window = parent
        self.original_opacity = self.config.get("opacity", 0.9)
        self.temp_text_color = self.config.get("text_color")
        self.temp_bg_color = self.config.get("bg_color")

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
        layout.addRow("字体大小:", self.font_spin)

        self.font_combo = QFontComboBox()
        current_font_family = self.config.get("font_family", "Microsoft YaHei")
        self.font_combo.setCurrentFont(QFont(current_font_family))
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

        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(PAGE_MARGIN_MIN, PAGE_MARGIN_MAX)
        self.margin_spin.setSingleStep(2)
        self.margin_spin.setSuffix(" px")
        self.margin_spin.setValue(clamp_config_int(self.config, "page_margin", 0,
                                                   PAGE_MARGIN_MIN, PAGE_MARGIN_MAX))
        self.margin_spin.setToolTip("文字四周的留白（上下左右相同）。窗口很窄时别设太大，会挤掉每行字数。")
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
                                       "敲键盘或点鼠标就退出并藏回去。热键本身不会被发给当前应用。")
        layout.addRow("偷瞄热键:", self.peek_key_input)

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

    def on_typography_change(self, _value=None):
        """行距/段间距/页边距/首行缩进都是拖动即预览。"""
        self.config["line_spacing"] = self.spacing_spin.value()
        self.config["paragraph_spacing"] = self.para_spin.value()
        self.config["page_margin"] = self.margin_spin.value()
        self.config["first_line_indent"] = self.indent_spin.value()
        self.config["paragraph_indent"] = self.para_indent_check.isChecked()
        if self.main_window:
            self.main_window.apply_style()

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

    def accept(self):
        self.config["ip"] = self.ip_input.text().strip()
        self.config["font_size"] = self.font_spin.value()
        self.config["font_family"] = self.font_combo.currentFont().family()
        self.config["line_spacing"] = self.spacing_spin.value()
        self.config["paragraph_spacing"] = self.para_spin.value()
        self.config["page_margin"] = self.margin_spin.value()
        self.config["first_line_indent"] = self.indent_spin.value()
        self.config["paragraph_indent"] = self.para_indent_check.isChecked()
        self.config["boss_key"] = self.boss_key_input.text().strip()
        self.config["peek_key"] = self.peek_key_input.text().strip()
        self.config["text_color"] = self.temp_text_color
        self.config["bg_color"] = self.temp_bg_color
        self.config["ghost_mode"] = self.check_ghost_mode.isChecked()
        self.config["auto_mode"] = self.config["auto_mode"]  # 修复：这里应该是 self.check_auto_mode.isChecked()
        self.config["auto_mode"] = self.check_auto_mode.isChecked()
        self.config["antishot_mode"] = self.check_antishot.isChecked()
        super().accept()

    def reject(self):
        self.config["opacity"] = self.original_opacity
        if self.main_window:
            self.main_window.apply_style()
            set_window_protection(int(self.main_window.winId()), self.config.get("antishot_mode", False))
        super().reject()


# ================= 主程序 =================
class StealthReader(QWidget):
    update_text_signal = pyqtSignal(str, bool)
    hotkey_signal = pyqtSignal()
    peek_hotkey_signal = pyqtSignal()
    # 偷瞄模式的钩子回调只发信号（QueuedConnection），翻页和退出都回到主线程做
    peek_wheel_signal = pyqtSignal(int)
    peek_exit_signal = pyqtSignal(str)
    bookshelf_updated_signal = pyqtSignal(list)

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
        self.local_chapters = []  # epub 章节目录 [(标题, 起始字符偏移)]，txt 时为空
        self.local_heading_offsets = set()  # 标题行的字符偏移，首行缩进要跳过它们
        self.displaying_local_text = False  # 当前显示的文本是否来自本地书（防止标题表串到网络章节上）

        # --- 偷瞄模式（Ctrl+E 浮现 + 全局滚轮翻页）---
        self.peek_mode = False
        self.peek_was_visible = False   # 进入偷瞄前窗口是否可见，退出时照原样恢复
        self.peek_last_toggle = 0.0     # 和老板键一样防连击
        self.peek_last_exit = 0.0       # 刚退出的一瞬，Ctrl+E 的余波不再当新指令
        self.peek_shortcut = None
        self.peek_input_hook = GlobalInputHook(self)

        # 老板键看门狗：keyboard 库的监听线程偶尔会静默死亡（锁屏/睡眠/钩子被系统强拆），
        # 每 45 秒查一次，死了就重新注册
        self.hotkey_watchdog = QTimer(self)
        self.hotkey_watchdog.setInterval(45000)
        self.hotkey_watchdog.timeout.connect(self.check_hotkeys_alive)
        self.hotkey_watchdog.start()

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

        self.initUI()
        self.initTray()

        self.update_text_signal.connect(self.on_update_text_safe)
        self.hotkey_signal.connect(self.toggle_window)
        self.peek_hotkey_signal.connect(self.toggle_peek)
        self.peek_wheel_signal.connect(self.on_peek_wheel, Qt.QueuedConnection)
        self.peek_exit_signal.connect(self.on_peek_exit, Qt.QueuedConnection)
        self.bookshelf_updated_signal.connect(self.on_bookshelf_updated)

        self.refresh_hotkeys()

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
                # 纯文本没有结构信息，只能按字面规则猜标题行
                headings = detect_txt_headings(content)

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

    def page_margin_px(self):
        return clamp_config_int(self.config, "page_margin", 0,
                                PAGE_MARGIN_MIN, PAGE_MARGIN_MAX)

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
        char_width = QFontMetrics(self.text_edit.font()).horizontalAdvance("\u3000")
        return int(round(char_width * self.first_line_indent_chars()))

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
        if abs(document.documentMargin() - margin) > 0.5:
            document.setDocumentMargin(margin)

        block_format = QTextBlockFormat()
        block_format.setLineHeight(self.line_spacing_percent(), QTextBlockFormat.ProportionalHeight)
        block_format.setTopMargin(self.paragraph_spacing_px())
        block_format.setTextIndent(self.first_line_indent_px())

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

    # --- 核心：基于几何坐标探测下一页起始位置 ---
    def calc_next_page_start(self):
        """利用视图几何坐标，探测屏幕底部边缘的字符位置"""
        # 【新增保护】防止空内容计算
        if not self.text_edit.toPlainText():
            return 0

        viewport_h = self.text_edit.viewport().height()
        # 探测点：视图左下角再往下一点点 (取下一行的开头)
        target_y = viewport_h + 2

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

            # 【关键】即时存档
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
        try:
            keyboard.unhook_all()
            keyboard.add_hotkey(hotkey_str, self.on_global_hotkey_triggered)
            # 注意：unhook_all 只拆 keyboard 库自己的钩子；偷瞄模式的 ctypes 钩子独立管理
            keyboard.add_hotkey(peek_key_str, self.on_peek_hotkey_triggered)
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

    def on_global_hotkey_triggered(self):
        log_event(f"老板键触发 boss_key={self.config.get('boss_key')}")
        self.hotkey_signal.emit()

    def on_peek_hotkey_triggered(self):
        self.peek_hotkey_signal.emit()

    def check_hotkeys_alive(self):
        """老板键看门狗：keyboard 库的监听线程死了就重新注册（锁屏/睡眠/钩子被系统强拆后常见）。"""
        try:
            listener = getattr(keyboard, "_listener", None)
            if listener is None or not listener.listening:
                return
            thread = getattr(listener, "listening_thread", None)
            if thread is not None and not thread.is_alive():
                log_event("老板键监听线程已死，重新注册全局热键")
                self.refresh_hotkeys()
        except Exception:
            pass

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

        self.content_layout.setContentsMargins(5, 0, 5, 0)
        self.content_layout.setSpacing(0)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFrameStyle(QFrame.NoFrame)
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_edit.setTextInteractionFlags(Qt.NoTextInteraction)
        self.text_edit.setFocusPolicy(Qt.NoFocus)

        self.text_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.text_edit.setMinimumHeight(0)
        self.text_edit.document().setDocumentMargin(0)

        self.text_edit.installEventFilter(self)

        self.content_layout.addWidget(self.text_edit)
        self.main_layout.addWidget(self.content_frame)
        self.setLayout(self.main_layout)

        w = self.config.get("window_width", 400)
        h = self.config.get("window_height", 300)
        self.resize(w, h)
        self.move(100, 100)
        self.oldPos = self.pos()

        self.apply_style()

    def eventFilter(self, source, event):
        if source == self.text_edit and event.type() == QEvent.Wheel:
            # 【关键保护】如果既没选书，也不是本地模式，直接拦截滚轮不处理
            if not self.is_local_mode and not self.current_book:
                return True

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

    def initTray(self):
        self.tray_icon = QSystemTrayIcon(self)
        icon = self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.tray_icon.setIcon(icon)
        tray_menu = QMenu()
        tray_menu.addAction("显示/隐藏").triggered.connect(self.toggle_window)
        tray_menu.addAction("退出").triggered.connect(self.quit_app)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.toggle_window()

    def toggle_window(self):
        current_time = time.time()
        if current_time - self.last_toggle_time < 0.3:
            return
        self.last_toggle_time = current_time

        if self.isVisible():
            log_event("老板键 -> 隐藏窗口")
            self.sync_progress_async()
            self.hide()
        else:
            log_event("老板键 -> 显示窗口")
            self.showNormal()
            self.apply_style()
            self.activateWindow()
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
        if not self.peek_input_hook.install(self.config.get("peek_key", "ctrl+e")):
            print("偷瞄模式未启动：全局输入钩子安装失败")
            return
        self.peek_was_visible = self.isVisible()
        self.peek_mode = True
        if not self.isVisible():
            self.show()  # WA_ShowWithoutActivating：不夺走工作窗口的焦点
        self.raise_()
        self.apply_style()
        if self.config.get("auto_mode", False):
            self.chameleon_timer.start()
            self.adjust_color_to_background()
        if self.config.get("antishot_mode", False):
            set_window_protection(int(self.winId()), True)

    def exit_peek(self, reason=""):
        if not self.peek_mode:
            return
        self.peek_mode = False
        self.peek_last_exit = time.time()
        self.peek_input_hook.uninstall()  # 先卸钩，后面任何一步出错都不能泄漏钩子
        if not self.peek_was_visible:
            # 跟着藏回去：退出即恢复按 Ctrl+E 之前的样子
            self.sync_progress_async()
            self.hide()
        else:
            self.apply_style()  # 本来就可见：恢复正常样式
            if self.config.get("ghost_mode", False):
                # 幽灵模式开着时，按当前光标位置重新评估淡出（leaveEvent 内部自带包含判断）
                self.leaveEvent(QEvent(QEvent.Leave))

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

            # 本地模式下修改样式需要重绘页面
            if self.is_local_mode:
                self.render_local_page()

    def enterEvent(self, event):
        self.is_mouse_in = True
        if self.config.get("ghost_mode", False):
            self.apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_mouse_in = False
        if self.peek_mode:
            return  # 偷瞄期间光标在外面是常态，别把窗口淡到透明
        if self.is_settings_open or self.is_resizing or self.is_moving: return

        global_pos = QCursor.pos()
        local_pos = self.mapFromGlobal(global_pos)
        if self.rect().contains(local_pos): return

        if self.config.get("ghost_mode", False):
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
        if not self.current_book or self.is_local_mode: return
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
                new_w = max(event.pos().x(), 100)
                min_h = getattr(self, 'single_line_height', 20)
                new_h = max(event.pos().y(), min_h)
                self.resize(new_w, new_h)

                # 【核心逻辑】调整大小时基于锚点重绘
                if self.is_local_mode:
                    self.render_local_page()

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

    def contextMenuEvent(self, event):
        cmenu = QMenu(self)
        cmenu.addAction("📂 打开本地书籍 (TXT/EPUB)").triggered.connect(self.open_local_file_dialog)
        toc_action = cmenu.addAction("📑 本地目录")
        toc_action.triggered.connect(self.open_local_toc)
        toc_action.setEnabled(bool(self.is_local_mode and self.local_chapters))
        cmenu.addSeparator()
        cmenu.addAction("📚 网络书架 (搜索)").triggered.connect(self.open_book_selector)
        cmenu.addAction("📖 章节目录 (网络)").triggered.connect(self.open_toc_selector)
        cmenu.addSeparator()
        cmenu.addAction("⚙️ 设置").triggered.connect(self.open_settings)
        cmenu.addSeparator()
        cmenu.addAction("❌ 退出").triggered.connect(self.quit_app)
        cmenu.exec_(self.mapToGlobal(event.pos()))

    def open_local_toc(self):
        if not self.local_chapters:
            return
        dialog = LocalTocSelector(self.local_chapters, self.local_start_index, self)
        if dialog.exec_() == QDialog.Accepted and dialog.selected_offset is not None:
            self.local_page_history = []
            self.local_start_index = max(0, int(dialog.selected_offset))
            self.config["last_local_pos"] = self.local_start_index
            self.save_config()
            self.render_local_page()

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
        if key in [Qt.Key_Right, Qt.Key_Down, Qt.Key_Space, Qt.Key_PageDown]:
            self.scroll_page(1)
        elif key in [Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp]:
            self.scroll_page(-1)

    def closeEvent(self, event):
        self.exit_peek("close")  # 窗口关了必须先卸全局钩子，再走保存
        self.sync_progress_async()
        if self.is_local_mode:
            self.config["last_local_pos"] = self.local_start_index
            self.save_config()
        super().closeEvent(event)

    def quit_app(self):
        # 退出前强制保存本地进度
        if self.is_local_mode:
            self.config["last_local_pos"] = self.local_start_index
            self.save_config()

        self.peek_input_hook.uninstall()  # 钩子泄漏的症状是全系统滚轮发卡，退出必须拆干净
        keyboard.unhook_all()
        QApplication.instance().quit()


if __name__ == '__main__':
    # 单实例：双开会导致两个进程同时响应老板键（看起来像"唤醒不了"），并且互相覆盖
    # config 里的阅读进度（看起来像"进度跳位"）。用命名互斥体挡住第二个进程。
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "StealthReader_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "StealthReader 已经在运行了（托盘里）", "提示", 0x40)
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ex = StealthReader()
    ex.show()
    log_event("应用启动")
    sys.exit(app.exec_())