# -*- coding: utf-8 -*-
"""AnkiConnect 客户端 —— 只走 HTTP，绝不读写 collection.anki2。

规矩全部是在本机（Anki 26.9.3 + AnkiConnect）实测出来的，不是照抄文档：

1. 信封是 AnkiConnect 自己的 `{action, version, params[, key]}`，**不是 JSON-RPC 2.0**
   （只有它的新 `/api/v1` 才是那个）；而且必须显式带 `version=6`——省略会被当作 v4 并返回裸结果。
2. **实参必须嵌在 `params` 里**。扁平写法会被静默忽略：`findCards` 返回空列表、
   `getDeckStats` 报 "missing 1 required positional argument"。
3. **绕开代理**。本机有代理会插手 localhost 请求（对任何未监听端口都回 HTTP 502）：
   不绕开一是连不上时错误分类走偏，二是卡片内容可能经过第三方。
4. `gui*` 在不活跃/未就绪时有**两种**表现：抛 `invalidState`，或**返回 `False`**——
   两者都必须当失败处理（`guiAnswerCard` 在"这张卡没提供该按钮"时也是返回 False）。
5. `guiAnswerCard` / `guiUndo` 是**异步 accepted**（官方原文：accepted 只表示"开始干活了"）：
   评分后紧跟一次 `guiCurrentCard` 会读到还没翻过去的那张，所以评分+取下一张走 `multi` 链式调用。
6. **隐私**：卡片正文、答案、音频文件名一律不进日志、不进异常文本——只要落一处盘，
   这个"隐形"功能的材料就等于躺在磁盘上。

对应设计：docs/anki-review-design.md；计划：docs/anki-review-plan.md（批次 1）。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8765"

# 给用户看的安装指引（连不上时原样贴出去）
PLUGIN_CODE = "2055492159"
INSTALL_HINT = (
    "连不上 AnkiConnect（Anki 没开，或插件没装）。\n"
    f"装法：Anki → 工具 → 插件 → 获取插件 → 填 {PLUGIN_CODE} → 重启 Anki。\n"
    "（新版 AnkiConnect 要求 Anki ≥ 26.8）"
)

# 错误文本判定。不同版本 AnkiConnect 的文案会漂，所以按"特征片段"判，不写死整句。
NOT_REVIEWING_MARKS = ("Gui review is not currently active.",
                       "reviewer is not available",
                       "invalidState: reviewer")
BUSY_MARKS = ("busy:", "collection operation is active")
API_KEY_MARK = "valid api key"


class AnkiError(Exception):
    """AnkiConnect 相关错误的基类。"""


class AnkiUnavailable(AnkiError):
    """连不上：Anki 没开、插件没装、端口被占。"""


class AnkiNotReviewing(AnkiError):
    """Anki 的 GUI 当前不在复习界面（需要先 guiDeckReview），或者今天已经没卡了。"""


class AnkiBusy(AnkiError):
    """Anki 正在干别的（同步 / 媒体检查 / 导入 / 查库），此时它会把所有 gui* 拦下来。

    这种时候要退避重试并明确告诉用户，**绝不能把用户的评分悄悄吞掉**。
    """


class AnkiNeedsKey(AnkiError):
    """AnkiConnect 配了 apiKey，请求需要带上 key。"""


class AnkiCommandError(AnkiError):
    """其它明确错误（插件版本不支持、查询语法错、内部异常等）。"""


class AnkiClient:
    def __init__(self, url=DEFAULT_URL, timeout_read=4.0, timeout_gui=12.0, api_key=None):
        self.url = url
        self.timeout_read = timeout_read
        self.timeout_gui = timeout_gui
        self.api_key = api_key
        # 绕开代理（见模块开头第 3 条）
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._media_dir_cache = None
        self._anki_hwnd_cache = None
        self._anki_was_concealed = False

    # ---------- 底层 ----------

    def invoke(self, action, params=None, timeout=None, gui=False):
        """发一次请求。信封见模块开头第 1 条，实参见第 2 条。"""
        payload = {"action": action, "version": 6}
        if params:
            payload["params"] = params
        if self.api_key:
            payload["key"] = self.api_key
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        used = timeout if timeout is not None else (self.timeout_gui if gui else self.timeout_read)
        try:
            with self._opener.open(req, timeout=used) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:                      # 端口通、但回的是 HTTP 错误
            raise AnkiCommandError(f"{action} HTTP {e.code}") from e
        except Exception as e:                                    # 连接被拒/超时/DNS
            raise AnkiUnavailable(f"{action} 连不上 {self.url}: {type(e).__name__}") from e
        try:
            data = json.loads(raw)
        except ValueError as e:                                   # 端口上的东西不是 AnkiConnect
            raise AnkiUnavailable(f"{action}: {self.url} 的响应不是 AnkiConnect") from e
        err = data.get("error")
        if err:
            raise self._classify(action, err)
        return data.get("result")

    @staticmethod
    def _classify(action, err):
        """把 AnkiConnect 的错误文本归类。注意：异常里只放 action 名与错误本身，绝不含卡片内容。"""
        text = str(err)
        if any(m in text for m in BUSY_MARKS):
            return AnkiBusy(f"{action}: Anki 正忙（{text}）")
        if API_KEY_MARK in text:
            return AnkiNeedsKey(f"{action}: AnkiConnect 需要 apiKey（{text}）")
        if any(m in text for m in NOT_REVIEWING_MARKS):
            return AnkiNotReviewing(f"{action}: {text}")
        return AnkiCommandError(f"{action}: {text}")

    def ping(self):
        """存活探测。注意：AnkiConnect **没有** `ping` 这个 action，这里走的是 `version`。"""
        return self.invoke("version")

    def api_reflect(self, scopes=("actions",)):
        """列 AnkiConnect 支持的动作（本机 122 个），用来判断插件这一代有没有我们要的 gui*。"""
        return self.invoke("apiReflect", {"scopes": list(scopes)})

    def review_active(self):
        """Anki 的复习器现在是不是活跃的。比拿错误文案猜"今天没卡了"可靠得多。"""
        return bool(self.invoke("guiReviewActive"))

    def active_profile(self):
        return self.invoke("getActiveProfile")

    # ---------- 只读：牌组与计数 ----------

    def deck_names(self):
        return self.invoke("deckNames") or []

    def deck_names_and_ids(self):
        return self.invoke("deckNamesAndIds") or {}

    def deck_stats(self, decks=None):
        """{牌组名: {"new": n, "learn": n, "review": n, "total": n}}。

        这几个数是 Anki 按**每日上限**算好的，比 findCards 现算更贴近"今天还欠几张"
        （库里 1,195 张到期、默认上限 200 时，findCards('is:due') 会一直说 1195，
        而复习器其实早就"今日已清"，两个真相源会打架）。
        父牌组的数已经含子牌组（Anki 自己的口径），所以选父牌组就是对整棵子树复习。

        **一个牌组查不到计数，整批就全废**（实测：本机「系统默认」单独查也报
        "Deck counts unavailable."，而 26 个牌组一起查会因为它整批失败——"列出牌组"
        按钮当时就是这么变成"读不到牌组"的）。所以批量失败后逐个重试，查得到的收下、
        查不到的跳过；调用方按"这个牌组没有计数"处理，不要当整体失败。
        """
        if decks is None:
            decks = self.deck_names()
        decks = list(decks)
        if not decks:
            return {}
        raw = {}
        try:
            raw = self.invoke("getDeckStats", {"decks": decks}) or {}
        except AnkiCommandError:
            for name in decks:                       # 有坏牌组就整批废掉，退化成逐个查
                try:
                    raw.update(self.invoke("getDeckStats", {"decks": [name]}) or {})
                except AnkiCommandError:
                    continue
        out = {}
        for item in raw.values():
            out[item.get("name", "")] = {
                "new": int(item.get("new_count", 0) or 0),
                "learn": int(item.get("learn_count", 0) or 0),
                "review": int(item.get("review_count", 0) or 0),
                "total": int(item.get("total_in_deck", 0) or 0),
            }
        return {k: v for k, v in out.items() if k}

    def remaining(self, deck):
        """今天这个牌组还欠几张（已含每日上限口径）。"""
        stat = self.deck_stats([deck]).get(deck)
        if not stat:
            return None
        return stat["new"] + stat["learn"] + stat["review"]

    def reviewed_today(self):
        """Anki 自己记的今日已复习数（'本次已背 M' 一律以它为准，别用本地内存计数）。"""
        return int(self.invoke("getNumCardsReviewedToday") or 0)

    def find_cards(self, query):
        """Anki 搜索语法。注意 `deck:X` 会连子牌组一起命中；返回 id 列表，别频繁调全库查询。"""
        return self.invoke("findCards", {"query": query}) or []

    # ---------- GUI：镜像 Anki 自己的复习器 ----------

    def start_review(self, deck):
        """让 Anki 自己打开这个牌组的复习界面。

        注意：**牌组名不存在时它返回 False 而不抛异常**（实测），所以这里必须判返回值，
        否则牌组名写错会静默落进"没卡了"分支。它还会把 Anki 窗口顶到前台一次。
        """
        res = self.invoke("guiDeckReview", {"name": deck}, gui=True)
        if res is False:
            raise AnkiCommandError(f"start_review: Anki 里没有牌组 {deck!r}（guiDeckReview 返回 False）")
        return res

    def leave_review(self):
        """回到牌组浏览界面。"""
        return self.invoke("guiDeckBrowser", gui=True)

    def current_card(self):
        """当前这张卡：cardId / question / answer / css / modelName / deckName / buttons / nextReviews / fields。

        两个坑（都实测过）：
        - **`answer` 在没翻面时就已经渲染好了**（2124 / 2377 字符都非空），所以"现在该显示正面
          还是反面"要看我们自己的状态，绝不能拿 `answer == ""` 判断——否则正面就把答案亮出来了。
        - Anki 说没卡了、或复习器不活跃，都会抛 `AnkiNotReviewing`；要让"今日已清"的判断可靠，
          配合 `review_active()` 与 `deck_stats()`。
        """
        card = self.invoke("guiCurrentCard", gui=True)
        if not card:
            raise AnkiNotReviewing("Anki 没给出当前卡片")
        return card

    def show_answer(self):
        res = self.invoke("guiShowAnswer", gui=True)
        if res is False:                     # 不活跃时返回 False 而不是报错（实测）
            raise AnkiNotReviewing("guiShowAnswer 返回 False（复习器不活跃）")
        return res

    def show_question(self):
        res = self.invoke("guiShowQuestion", gui=True)
        if res is False:
            raise AnkiNotReviewing("guiShowQuestion 返回 False（复习器不活跃）")
        return res

    def play_audio(self):
        """播放当前这一面的音频。Anki 自己处理 `[sound:]`、`{{tts:}}` 与自动播放策略，
        比我们自己抽标签再找播放器更准（我们要做的只是把标签从显示文本里删掉）。"""
        res = self.invoke("guiPlayAudio", gui=True)
        if res is False:
            raise AnkiNotReviewing("guiPlayAudio 返回 False（复习器不活跃）")
        return res

    def start_card_timer(self):
        """让 Anki 从"卡片由 API 显示出来"的这一刻开始计时，这样答题用时才准。"""
        res = self.invoke("guiStartCardTimer", gui=True)
        if res is False:
            raise AnkiNotReviewing("guiStartCardTimer 返回 False（复习器不活跃）")
        return res

    def answer(self, ease):
        """评分。ease 是 Anki 实际提供的按钮序号（看 `current_card()["buttons"]`，通常是 [1,2,3,4]，
        学习步/FSRS 下可能只有 2 个）。

        返回 True 表示 Anki 接受了；**返回 False 表示这张卡没提供该按钮、评分没有生效**，
        必须当"未评分"处理，不能当成功继续往下走。
        """
        if int(ease) not in (1, 2, 3, 4):
            raise ValueError(f"ease 只能是 1-4，收到 {ease}")
        res = self.invoke("guiAnswerCard", {"ease": int(ease)}, gui=True)
        return res is not False

    def undo(self):
        """Anki 的**全局**撤销栈（collection.undo），不只是撤销上一次评分。"""
        return self.invoke("guiUndo", gui=True)

    def wait_card_change(self, previous_id, timeout=4.0, interval=0.15):
        """等当前卡换掉（评分是异步的）。返回新卡；Anki 说没卡了返回 None；超时抛 AnkiNotReviewing。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                card = self.current_card()
            except AnkiNotReviewing:
                return None                      # 没卡了 = 今日清完
            if card.get("cardId") != previous_id:
                return card
            time.sleep(interval)
        raise AnkiNotReviewing(f"评分后 {timeout}s 内卡片没换（cardId 仍是 {previous_id}）")

    def answer_and_next(self, ease, previous_id):
        """评分 + 取下一张，返回 (是否被接受, 新卡或 None)。

        `guiAnswerCard` 是异步 accepted，紧跟一次 `guiCurrentCard` 会读到还没翻过去的那张
        （而且 cardId 相同还能骗过"一致性校验"），所以走 `multi` 链式调用等最终态；
        `multi` 不可用时退回单发 + 轮询。
        """
        actions = [{"action": "guiAnswerCard", "version": 6, "params": {"ease": int(ease)}},
                   {"action": "guiCurrentCard", "version": 6}]
        try:
            res = self.invoke("multi", {"actions": actions}, gui=True)
        except AnkiBusy:
            raise
        except AnkiError:
            accepted = self.answer(ease)
            return accepted, (self.wait_card_change(previous_id) if accepted else None)
        if not isinstance(res, list) or len(res) < 2:
            accepted = self.answer(ease)
            return accepted, (self.wait_card_change(previous_id) if accepted else None)
        first, second = res[0] or {}, res[1] or {}
        if first.get("error"):
            err = str(first["error"])
            if any(m in err for m in BUSY_MARKS):
                raise AnkiBusy(f"guiAnswerCard: Anki 正忙（{err}）")
            raise AnkiCommandError(f"guiAnswerCard: {err}")
        accepted = first.get("result") is not False
        card = second.get("result")
        if not accepted:
            return False, self.current_card()
        if card is None:
            if second.get("error"):
                raise self._classify("guiCurrentCard", second["error"])
            return True, None                    # 今日已清（比如刚好答完最后一张）
        return True, card

    # ---------- 媒体 ----------

    def media_dir(self):
        """collection.media 的绝对路径。

        优先问 Anki 自己（`getMediaDirPath` 给的是**当前打开的那个配置档**的目录）——
        自己扫 `%APPDATA%\\Anki2\\*` 在多配置档时必然选错。问不到才退回扫描。
        """
        if self._media_dir_cache and Path(self._media_dir_cache).is_dir():
            return self._media_dir_cache
        try:
            d = self.invoke("getMediaDirPath")
            if d and Path(d).is_dir():
                self._media_dir_cache = Path(d)
                return self._media_dir_cache
        except AnkiError:
            pass
        root = Path(os.environ.get("APPDATA", "")) / "Anki2"
        cands = [p for p in root.glob("*/collection.media") if p.is_dir()]
        if not cands:
            return None
        self._media_dir_cache = max(cands, key=lambda p: p.stat().st_mtime)
        return self._media_dir_cache

    def media_path(self, name):
        """媒体文件名 → 本地绝对路径（图片直接给 QTextDocument 当 baseUrl 用）。

        只走目录直读：452 MB 的媒体库如果退化成 base64 传输会明显卡住界面。
        """
        d = self.media_dir()
        if not d or not name:
            return None
        p = d / name
        return p if p.is_file() else None

    def retrieve_media(self, name):
        """兜底：让 AnkiConnect 把文件内容以 base64 传回来。

        注意文件不存在时它返回的是 **`False`**（不是空串），实测如此。
        """
        b64 = self.invoke("retrieveMediaFile", {"filename": name})
        if b64 is False or not b64:
            return None
        try:
            return base64.b64decode(b64)
        except Exception:
            return None

    # ---------- Windows：把 Anki 自己的窗口藏起来 ----------

    def anki_pid(self):
        """用 toolhelp 快照找 Anki.exe 的 pid（不依赖 psutil）。"""
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID = ctypes.c_void_p(-1).value

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID:
            return None
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not k32.Process32First(snap, ctypes.byref(entry)):
                return None
            while True:
                if entry.szExeFile.decode("mbcs", "ignore").lower() == "anki.exe":
                    return entry.th32ProcessID
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    return None
        finally:
            k32.CloseHandle(snap)

    def anki_hwnd(self):
        """Anki 主窗口句柄。

        实测（Anki 26.9.3 / Qt 6.11）：Anki 的顶层窗口有 8 个，主窗口类名形如
        `Qt6112QWindowIcon`（版本号会变，按后缀匹配），其余是 `...ThemeChangeObserverWindow`
        / `...ScreenChangeObserverWindow` / `Chrome_WidgetWin_0` / `IME` 之类的空壳。
        窗口最小化时 `GetWindowRect` 返回 `157x25@-16000,-16000` 这种垃圾值，
        **尺寸必须看 `GetWindowPlacement` 的 `rcNormalPosition`**（这个坑实测踩过一次）。
        """
        if self._anki_hwnd_cache and ctypes.windll.user32.IsWindow(self._anki_hwnd_cache):
            return self._anki_hwnd_cache
        pid = self.anki_pid()
        if not pid:
            return None

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wt.UINT), ("flags", wt.UINT), ("showCmd", wt.UINT),
                        ("ptMinPosition", wt.POINT), ("ptMaxPosition", wt.POINT),
                        ("rcNormalPosition", wt.RECT)]

        u32 = ctypes.windll.user32
        cands = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, _):
            owner = wt.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            if not cls.value.endswith("QWindowIcon"):        # Qt 的"真窗口"类
                return True
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
            if u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                r = wp.rcNormalPosition
            else:
                r = wt.RECT()
                u32.GetWindowRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > 100000:                                # 主窗口；过滤掉 0x0 的空壳
                cands.append((area, hwnd))
            return True

        u32.EnumWindows(cb, 0)
        self._anki_hwnd_cache = max(cands)[1] if cands else None
        return self._anki_hwnd_cache

    def anki_is_foreground(self):
        hwnd = self.anki_hwnd()
        return bool(hwnd) and ctypes.windll.user32.GetForegroundWindow() == hwnd

    def conceal_anki(self):
        """把 Anki 自己的窗口**彻底藏起来**（不是最小化）。

        实测：`SW_HIDE` 之后 AnkiConnect 的 GUI 动作照常可用（`guiCurrentCard` 还能取到同一张卡），
        而隐藏的窗口不进 Alt+Tab、没有任务栏图标、任务栏悬停也没有缩略图——最小化做不到这点
        （最小化的窗口会被人从身后按 Alt+Tab 看到最后那一帧，也就是那张卡）。
        返回 True 表示"确实是我们藏的"，退出时才需要还原。
        """
        hwnd = self.anki_hwnd()
        if not hwnd:
            self._anki_was_concealed = False
            return False
        u32 = ctypes.windll.user32
        if not u32.IsWindowVisible(hwnd):
            self._anki_was_concealed = False      # 本来就看不见，别去动它
            return False
        u32.ShowWindow(hwnd, 0)              # SW_HIDE
        self._anki_was_concealed = True
        return True

    def reveal_anki(self, activate=False):
        """还原我们藏起来的 Anki 窗口。

        **默认不激活**：`SW_SHOW`/`SW_RESTORE` 会把 Anki 亮到最前面，正好把假文档暴露给同事。

        两个实测过的坑（都出现过"还原了但人还是看不见"）：
        1. 藏之前 Anki 是**最小化**的话，`SW_SHOWNOACTIVATE` 不会把它展开——还原完还得查一次
           `IsIconic`，仍然缩着就 `SW_RESTORE`（这一步会激活窗口，但"看不见"比"跳一下"严重）。
        2. 最小化窗口的 `GetWindowRect` 是 `@-16000,-16000` 这种垃圾值，所以位置体检查的是
           `GetWindowPlacement` 的 `rcNormalPosition`；整个落在屏幕外就搬回主屏居中。
        """
        if not self._anki_was_concealed:
            return False
        hwnd = self.anki_hwnd()
        self._anki_was_concealed = False
        if not hwnd:
            return False
        u32 = ctypes.windll.user32
        u32.ShowWindow(hwnd, 9 if activate else 4)   # SW_RESTORE / SW_SHOWNOACTIVATE
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)                  # 还缩着/还藏着：展开
        self.ensure_on_screen(hwnd)
        return True

    def ensure_anki_visible(self, activate=False):
        """无条件把 Anki 的主窗口叫回屏幕内（托盘菜单 / 上次被杀之后的兜底）。

        和 `reveal_anki` 不同：不看"是不是我们藏的"，只看结果——看不见就显示，在屏幕外就搬回来。
        """
        hwnd = self.anki_hwnd()
        if not hwnd:
            return False
        u32 = ctypes.windll.user32
        moved = False
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9 if activate else 4)
            if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
                u32.ShowWindow(hwnd, 9)
            moved = True
        if self.ensure_on_screen(hwnd):
            moved = True
        self._anki_was_concealed = False
        return moved

    def ensure_on_screen(self, hwnd):
        """窗口整个落在所有显示器之外时，搬回主屏工作区居中。返回是否搬过。

        判据用 `GetWindowPlacement().rcNormalPosition`（最小化窗口的 `GetWindowRect` 是垃圾值），
        并逐显示器判交集——双屏下摆在副屏是正常的，不该被搬走。
        """
        u32 = ctypes.windll.user32

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wt.UINT), ("flags", wt.UINT), ("showCmd", wt.UINT),
                        ("ptMinPosition", wt.POINT), ("ptMaxPosition", wt.POINT),
                        ("rcNormalPosition", wt.RECT)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return False
        r = wp.rcNormalPosition

        hit = [False]
        MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HANDLE,
                                            ctypes.POINTER(wt.RECT), wt.LPARAM)

        def _cb(hmon, _hdc, _lprc, _lp):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                m = mi.rcMonitor
                if (r.right > m.left and r.left < m.right
                        and r.bottom > m.top and r.top < m.bottom):
                    hit[0] = True
                    return False
            return True

        u32.EnumDisplayMonitors(0, None, MONITORENUMPROC(_cb), 0)
        if hit[0]:
            return False                                    # 在某个显示器上，别动它
        work = wt.RECT()
        u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)   # SPI_GETWORKAREA 主屏
        avail_w = work.right - work.left
        avail_h = work.bottom - work.top
        w = max(640, min(r.right - r.left, avail_w - 80))
        h = max(480, min(r.bottom - r.top, avail_h - 80))
        x = work.left + (avail_w - w) // 2
        y = work.top + (avail_h - h) // 2
        SWP_NOZORDER, SWP_SHOWWINDOW = 0x0004, 0x0040
        u32.SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_SHOWWINDOW)
        return True

    # 兼容旧名（批次 1 探针用过）
    minimize_anki = conceal_anki
    restore_anki = reveal_anki


def status_text(client):
    """给界面/日志用的一行状态：'AnkiConnect 6 · 26 个牌组 · 0.12s' 或失败原因。

    只输出版本号、数量、耗时与错误类型——**不含任何卡片内容**。
    """
    t0 = time.time()
    try:
        ver = client.ping()
    except AnkiUnavailable:
        return INSTALL_HINT
    except AnkiError as e:
        return f"AnkiConnect 异常（{type(e).__name__}）"
    try:
        decks = len(client.deck_names())
    except AnkiError:
        decks = "?"
    return f"AnkiConnect {ver} · {decks} 个牌组 · {time.time() - t0:.2f}s"
