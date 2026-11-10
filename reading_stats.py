# -*- coding: utf-8 -*-
"""阅读统计：会话心跳 → 日账 / 书账 / 会话表 → 聚合出时长、速度、连续天数、热力图。

**纯标准库，不碰 Qt**：这一层要能在探针里直接跑、也要能离线改数据，
界面（`ReadingStatsDialog`）只负责把它算好的数字画漂亮。

数据落在 `reading_stats.json`（和 `config.json` 同目录，但**不放进 config**：
config 每次保存都整体重写，日账塞进去早晚被冲掉）：

    {
      "version": 1,
      "days":     {"2026-11-10": {"sec": 1830, "chars": 12000, "n": 4}},
      "books":    {"<书的键>": {"title": "…", "sec": 7200, "chars": 48000, "n": 14,
                               "first": 1762000000, "last": 1762700000,
                               "days": {"2026-11-09": {"sec": 900, "chars": 6000}}}},
      "sessions": [{"k": "<书的键>", "title": "…", "start": 1762700000, "sec": 900, "chars": 6000}]
    }

几条口径先写死（免得以后各处各算一套）：
  * 一次会话算在**开始那一刻**所在的日期里。跨零点读通宵会全算前一天，
    这是刻意的：宁可少一天，也不要同一次阅读被劈成两段、两天的均值都失真。
  * 字数只累加**正增量**：往回翻、跳章会算出负数，那不是"读掉了"。
  * 速度一律按 `字数 / 分钟`，秒数不足 10 秒的会话不参与速度统计（除零/离谱值）。
"""
import json
import os
import time

VERSION = 1
STATS_NAME = "reading_stats.json"

TICK_SEC = 5              # 心跳粒度：5 秒一次（界面那边就这么打）
IDLE_SEC = 180            # 3 分钟没有任何输入 = 人不在，停止计时
KEEP_SESSIONS = 400       # 会话表只留最近这么多条
KEEP_DAYS = 420           # 日账留一年多一点（热力图 53 周够用）
DAY_SEC_CAP = 18 * 3600   # 单日上限：机器挂了、时钟跳了都不至于记出 20 小时


# ---------------------------------------------------------------- 基础
def day_key(ts=None):
    """时间戳 → '2026-11-10'（本地时区）。"""
    return time.strftime("%Y-%m-%d", time.localtime(float(ts if ts is not None else time.time())))


def blank():
    return {"version": VERSION, "days": {}, "books": {}, "sessions": []}


def normalize(data):
    """把读回来的东西修成能用的样子：字段缺、类型错、旧版本都在这儿兜住。"""
    if not isinstance(data, dict):
        return blank()
    out = blank()
    days = data.get("days")
    if isinstance(days, dict):
        for k, v in days.items():
            row = _num_row(v)
            if row:
                out["days"][str(k)] = row
    books = data.get("books")
    if isinstance(books, dict):
        for k, v in books.items():
            if not isinstance(v, dict):
                continue
            book = _num_row(v)
            book["title"] = str(v.get("title") or "")
            book["first"] = _num(v.get("first"))
            book["last"] = _num(v.get("last"))
            book["days"] = {}
            sub = v.get("days")
            if isinstance(sub, dict):
                for dk, dv in sub.items():
                    row = _num_row(dv)
                    if row:
                        book["days"][str(dk)] = row
            out["books"][str(k)] = book
    sessions = data.get("sessions")
    if isinstance(sessions, list):
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sec = _num(s.get("sec"))
            if sec <= 0:
                continue
            out["sessions"].append({
                "k": str(s.get("k") or ""),
                "title": str(s.get("title") or ""),
                "start": _num(s.get("start")),
                "sec": sec,
                "chars": _num(s.get("chars")),
                "end": _num(s.get("end")),
            })
    return out


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    if n != n or n in (float("inf"), float("-inf")):     # NaN / inf
        return 0.0
    return n


def _num_row(v):
    if not isinstance(v, dict):
        return None
    sec = _num(v.get("sec"))
    chars = _num(v.get("chars"))
    n = _num(v.get("n"))
    if sec <= 0 and chars <= 0:
        return None
    return {"sec": sec, "chars": chars, "n": n}


# ---------------------------------------------------------------- 落盘
def load(path):
    """读统计文件；坏了就当空的，绝不让阅读器起不来。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return normalize(json.load(f))
    except Exception:
        return blank()


def save(path, data):
    """原子写：先写 .tmp 再 replace，半路被杀也不会留下半个 JSON。"""
    try:
        folder = os.path.dirname(os.path.abspath(path))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(normalize(data), f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 记账
def _reuse(data, fresh):
    """把修好的数据**写回调用方那个 dict**：调用方拿的是同一个对象，改完立刻可见。"""
    if not isinstance(data, dict):
        return fresh
    data.clear()
    data.update(fresh)
    return data


def add_span(data, key, title, start_ts, sec, chars, end_ts=None):
    """把一段阅读（一次会话）记进日账 / 书账 / 会话表。返回 data 本身（原地改）。"""
    data = _reuse(data, normalize(data))
    sec = max(0.0, _num(sec))
    chars = max(0.0, _num(chars))
    if sec <= 0 and chars <= 0:
        return data
    start = _num(start_ts) or time.time()
    end = _num(end_ts) or (start + sec)
    day = day_key(start)

    row = data["days"].setdefault(day, {"sec": 0.0, "chars": 0.0, "n": 0})
    row["sec"] = min(DAY_SEC_CAP, row["sec"] + sec)
    row["chars"] += chars
    row["n"] += 1

    key = str(key or "")
    book = data["books"].setdefault(key, {"title": "", "sec": 0.0, "chars": 0.0, "n": 0,
                                          "first": 0.0, "last": 0.0, "days": {}})
    if title:
        book["title"] = str(title)
    book["sec"] += sec
    book["chars"] += chars
    book["n"] += 1
    book["first"] = min(book["first"] or start, start)
    book["last"] = max(book["last"] or 0.0, end)
    sub = book["days"].setdefault(day, {"sec": 0.0, "chars": 0.0, "n": 0})
    sub["sec"] += sec
    sub["chars"] += chars
    sub["n"] += 1

    data["sessions"].append({"k": key, "title": str(title or ""), "start": start,
                             "sec": sec, "chars": chars, "end": end})
    data["sessions"] = data["sessions"][-KEEP_SESSIONS:]
    data["days"] = _trim(data["days"], KEEP_DAYS)
    return data


def _trim(table, keep):
    if len(table) <= keep:
        return table
    keys = sorted(table.keys())[-keep:]
    return {k: table[k] for k in keys}


# ---------------------------------------------------------------- 聚合
def speed(data, key=None):
    """字/分钟。只算够长的那几次（≥10 秒），一次 3 秒的误触不该把速度拉到天上。"""
    sec = chars = 0.0
    if key is None:
        for row in data.get("days", {}).values():
            sec += _num(row.get("sec"))
            chars += _num(row.get("chars"))
    else:
        book = (data.get("books") or {}).get(str(key)) or {}
        sec = sum(_num(r.get("sec")) for r in (book.get("days") or {}).values())
        chars = sum(_num(r.get("chars")) for r in (book.get("days") or {}).values())
    if sec < 10:
        return 0.0
    return chars / (sec / 60.0)


def active_days(data, key=None):
    """读过书的天数（不是日历跨度：中间停三个月不算）。"""
    if key is None:
        return sorted(data.get("days", {}).keys())
    book = (data.get("books") or {}).get(str(key)) or {}
    return sorted((book.get("days") or {}).keys())


def streak(data, key=None, now=None):
    """连续阅读天数：从今天往回数，今天没读就看昨天（昨天读了今天还没开始不算断）。"""
    have = set(active_days(data, key))
    if not have:
        return 0
    today = day_key(now)
    day = time.mktime(time.strptime(today, "%Y-%m-%d"))
    if day_key(day) not in have:
        day -= 86400                      # 今天还没读：从昨天开始数（今天还有机会）
        if day_key(day) not in have:
            return 0
    n = 0
    while day_key(day) in have:
        n += 1
        day -= 86400
    return n


def series(data, days=14, key=None, now=None):
    """最近 N 天，每天多少秒（从老到新，最后一个是今天）。"""
    have = {}
    if key is None:
        have = {k: _num(v.get("sec")) for k, v in data.get("days", {}).items()}
    else:
        book = (data.get("books") or {}).get(str(key)) or {}
        have = {k: _num(v.get("sec")) for k, v in (book.get("days") or {}).items()}
    base = time.mktime(time.strptime(day_key(now), "%Y-%m-%d"))
    out = []
    for i in range(max(1, int(days)) - 1, -1, -1):
        d = day_key(base - i * 86400)
        out.append((d, have.get(d, 0.0)))
    return out


def heatmap(data, weeks=53, key=None, now=None):
    """日历热力图的数据：`weeks` 列 × 7 行，每格 (日期, 秒数) 或 None（未来/越界）。"""
    have = {}
    if key is None:
        have = {k: _num(v.get("sec")) for k, v in data.get("days", {}).items()}
    else:
        book = (data.get("books") or {}).get(str(key)) or {}
        have = {k: _num(v.get("sec")) for k, v in (book.get("days") or {}).items()}
    today = time.mktime(time.strptime(day_key(now), "%Y-%m-%d"))
    # 右端对齐到今天，并且今天所在那一列的周日打头（Python: 周一=0）
    t = time.localtime(today)
    offset = (t.tm_wday + 1) % 7           # 周日=0 … 周六=6
    last_sunday = today - offset * 86400
    start = last_sunday - (weeks - 1) * 7 * 86400
    cols = []
    for w in range(weeks):
        col = []
        for d in range(7):
            ts = start + (w * 7 + d) * 86400
            if ts > today:
                col.append(None)
            else:
                k = day_key(ts)
                col.append((k, have.get(k, 0.0)))
        cols.append(col)
    return cols


def month_marks(cols):
    """热力图顶上的月份标签：[(列号, '11月')]，只在该月第一列出现时打一个。"""
    marks = []
    seen = ""
    for i, col in enumerate(cols):
        for cell in col:
            if not cell:
                continue
            day = cell[0]
            tag = day[:7]
            if tag != seen:
                seen = tag
                marks.append((i, "%d月" % int(day[5:7])))
            break
    return marks


def book_view(data, key, total=0, pos=0, now=None):
    """一本书的全套数字（界面直接用这坨）。"""
    data = normalize(data)
    book = (data.get("books") or {}).get(str(key)) or {}
    sec = sum(_num(r.get("sec")) for r in (book.get("days") or {}).values())
    chars = sum(_num(r.get("chars")) for r in (book.get("days") or {}).values())
    spd = speed(data, key)
    total = int(total or 0)
    pos = max(0, min(int(pos or 0), total or 10 ** 12))
    remain = max(0, total - pos)
    first = _num(book.get("first"))
    last = _num(book.get("last"))
    return {
        "key": str(key),
        "title": str(book.get("title") or ""),
        "sec": sec,
        "chars": chars,
        "sessions": int(_num(book.get("n"))),
        "speed": spd,
        "today_sec": _num((book.get("days") or {}).get(day_key(now), {}).get("sec")),
        "streak": streak(data, key, now),
        "active_days": len(active_days(data, key)),
        "first": first,
        "last": last,
        "span_days": int((last - first) // 86400) + 1 if first and last else 0,
        "total": total,
        "pos": pos,
        "remain": remain,
        "pct": (100.0 * pos / total) if total else 0.0,
        "remaining_sec": (remain / (spd / 60.0)) if spd > 0 and remain > 0 else 0.0,
    }


def overview(data, now=None):
    """全局数字：今日 / 本周 / 本月 / 总计 / 连续 / 速度 / 活跃天数。"""
    data = normalize(data)
    today = day_key(now)
    base = time.mktime(time.strptime(today, "%Y-%m-%d"))

    def _sum(since_ts):
        sec = chars = 0.0
        for k, row in data.get("days", {}).items():
            ts = time.mktime(time.strptime(k, "%Y-%m-%d"))
            if ts >= since_ts:
                sec += _num(row.get("sec"))
                chars += _num(row.get("chars"))
        return sec, chars

    total_sec = sum(_num(r.get("sec")) for r in data.get("days", {}).values())
    total_chars = sum(_num(r.get("chars")) for r in data.get("days", {}).values())
    day_sec, day_chars = _sum(base)
    week_sec, week_chars = _sum(base - 6 * 86400)
    month_sec, month_chars = _sum(base - 29 * 86400)
    days = active_days(data)
    return {
        "today_sec": day_sec, "today_chars": day_chars,
        "week_sec": week_sec, "week_chars": week_chars,
        "month_sec": month_sec, "month_chars": month_chars,
        "sec": total_sec, "chars": total_chars,
        "speed": speed(data),
        "streak": streak(data, None, now),
        "active_days": len(days),
        "first_day": days[0] if days else "",
        "last_day": days[-1] if days else "",
        "days": days,
    }


# ---------------------------------------------------------------- 排版小工具
def fmt_duration(sec, short=False):
    """3600*3+720 → '3 小时 12 分'；不到 1 分钟 → '不足 1 分'。"""
    sec = max(0, int(round(_num(sec))))
    if sec < 60:
        return "0 分" if short else "不足 1 分"
    minutes = sec // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return "%d 小时 %d 分" % (hours, minutes)
    if hours:
        return "%d 小时" % hours
    return "%d 分" % minutes


def fmt_speed(chars_per_min):
    """'480 字/分'；太少就说'--'。"""
    value = _num(chars_per_min)
    if value <= 0:
        return "--"
    if value >= 1000:
        return "%.1f 千字/分" % (value / 1000.0)
    return "%d 字/分" % int(round(value))


def fmt_day(ts):
    return day_key(ts) if _num(ts) else "--"


def fmt_clock(ts):
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return "--"


def fmt_gap(sec):
    """'3 天前' / '2 小时前'（书架和统计页共用一套说法）。"""
    sec = _num(sec)
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return "%d 分钟前" % int(sec // 60)
    if sec < 86400:
        return "%d 小时前" % int(sec // 3600)
    if sec < 86400 * 60:
        return "%d 天前" % int(sec // 86400)
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - sec))
