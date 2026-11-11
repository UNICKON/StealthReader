# -*- coding: utf-8 -*-
"""阅读统计的数据层：只记数、不算界面。纯标准库，不 import Qt。

这一层只做三件事：
1. **记账**：把"这一段在读"记进日账 / 书账 / 会话表（`add_span`）。
2. **聚合**：连续天数、速度、近 N 天序列、53 周热力图、今天的时间线、单本汇总。
3. **读写**：单独的 `reading_stats.json`，原子写；文件坏了当空的读。

口径（改这里一处，界面和探针都跟着变）：

* 心跳 5 秒一拍（`TICK_SEC`），一次"在读"的时长就是拍数 × 5。
* 3 分钟没有任何输入（翻页 / 滚轮 / 开书）就**先停止计时**（`IDLE_SEC`），但这一场先不收：
  盯着一页想事情不算离开。
* 同一本书**断到 45 分钟以上**才算这一场结束（`SESSION_GAP_SEC`）；断不到就并回同一场，
  中间那段不读的时间记成 `gaps`（场内空隙）。所以一场可以很长、中间带几段灰。
* 接不上上一场、本身又不到 1 分钟（`MIN_SESSION_SEC`）的整段不记：
  打开又关掉、随手瞟一眼那种不该占一条"会话"。
* 只累加**正增量**的字数，往回翻不扣；跨零点的通宵算在**开始那一刻**所在的日期里，
  但日账只按真实日期切分（跨零点的秒数全算开始那天，这是刻意的简化，写在这儿免得被当 bug）。
* 单日时长封顶 18 小时（`DAY_SEC_CAP`）：时钟跳变或休眠恢复时不会记出荒唐的数。

会话结构（`sessions` 里的一条）：

    {"k": 书键, "title": 书名, "start": 开始时间戳, "sec": 实际在读秒数,
     "chars": 字数, "end": 结束时间戳,
     "gaps": [[相对开始的偏移, 空隙秒数], ...]}   # 场内空隙，用来画时间线

时间线（`active_intervals`）＝ [开始, 结束] 减去所有 gaps，只用这两样就能画出
"哪几分钟在读、哪几分钟在歇"。
"""
import datetime
import json
import os
import time

VERSION = 1
STATS_NAME = "reading_stats.json"

TICK_SEC = 5              # 心跳粒度：5 秒一次（界面那边就这么打）
IDLE_SEC = 180            # 3 分钟没有任何输入 = 先停止计时（这一场先不收，人可能只是在看）
SESSION_GAP_SEC = 45 * 60  # 同一本书断 45 分钟以上才算这一场结束；不到就并回去
MIN_SESSION_SEC = 60      # 接不上上一场、本身又读了不到 1 分钟的：当成"没看"，整段不记
KEEP_SESSIONS = 400       # 会话表只留最近这么多场
KEEP_DAYS = 420           # 日账留这么久（约 14 个月，热力图一年多够看）
DAY_SEC_CAP = 18 * 3600   # 单日封顶：时钟跳变 / 休眠恢复时别记出荒唐的数
SLOT_SEC = 300            # 一天的时间线：5 分钟一格，288 格

WEEKDAYS = "一二三四五六日"


# ---------------------------------------------------------------- 小工具
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f in (float("inf"), float("-inf")):     # NaN / inf
        return 0.0
    return f


def day_key(ts=None):
    """时间戳 → 'YYYY-MM-DD'（本机时区）。"""
    if ts is None:
        return datetime.date.today().isoformat()
    try:
        return datetime.datetime.fromtimestamp(float(ts)).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime.date.today().isoformat()


def _day_start_epoch(day):
    """'YYYY-MM-DD' → 那天零点的时间戳（本机时区）。"""
    try:
        d = datetime.date.fromisoformat(str(day))
    except (TypeError, ValueError):
        d = datetime.date.today()
    return datetime.datetime.combine(d, datetime.time.min).timestamp()


def fmt_day(ts):
    """时间戳 → '2026-11-10'；没数就给 '--'。"""
    if not _num(ts):
        return "--"
    return day_key(ts)


def fmt_clock(ts):
    """时间戳 → '21:03'。"""
    if not _num(ts):
        return "--"
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "--"


def fmt_full(ts):
    """时间戳 → '11-10 21:03'（表里用，省地方）。"""
    if not _num(ts):
        return "--"
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "--"


def fmt_stamp(ts):
    """时间戳 → '2026-11-10 21:03:47'（悬停详情用）。"""
    if not _num(ts):
        return "--"
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "--"


def fmt_duration(sec):
    """秒 → '3 小时 12 分' / '10 分' / '不足 1 分'。"""
    sec = max(0.0, _num(sec))
    if sec < 60:
        return "不足 1 分"
    total_min = int(round(sec / 60.0))
    if total_min < 60:
        return "%d 分" % total_min
    return "%d 小时 %d 分" % (total_min // 60, total_min % 60)


def fmt_gap(sec):
    """空隙时长：和 fmt_duration 一个调子，但短的直接给秒。"""
    sec = max(0.0, _num(sec))
    if sec < 60:
        return "%d 秒" % int(round(sec))
    return fmt_duration(sec)


def fmt_speed(chars_per_min):
    """字/分 → '480 字/分' / '1.2 千字/分' / '--'。"""
    v = _num(chars_per_min)
    if v <= 0:
        return "--"
    if v >= 1000:
        return "%.1f 千字/分" % (v / 1000.0)
    return "%d 字/分" % int(round(v))


# ---------------------------------------------------------------- 结构
def blank():
    """空账本。"""
    return {"version": VERSION, "days": {}, "books": {}, "sessions": []}


def _num_row(row):
    if not isinstance(row, dict):
        return {"sec": 0.0, "chars": 0.0, "n": 0}
    return {"sec": max(0.0, _num(row.get("sec"))),
            "chars": max(0.0, _num(row.get("chars"))),
            "n": int(_num(row.get("n")))}


def _gaps(raw):
    """把任意脏数据收成 [[偏移, 时长], ...]，丢掉负数与非数。"""
    out = []
    if isinstance(raw, (list, tuple)):
        for pair in raw:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            off, dur = max(0.0, _num(pair[0])), max(0.0, _num(pair[1]))
            if dur >= 60:
                out.append([off, dur])
    out.sort(key=lambda p: p[0])
    return out[:200]


def normalize(data):
    """把账本收拾成"结构一定对"的样子：缺的补、脏的扔、超量的截。"""
    out = blank()
    if not isinstance(data, dict):
        return out
    days = data.get("days")
    if isinstance(days, dict):
        for k, row in days.items():
            key = str(k)
            if len(key) != 10:
                continue
            clean = _num_row(row)
            if clean["sec"] <= 0 and clean["chars"] <= 0:
                continue
            clean["sec"] = min(DAY_SEC_CAP, clean["sec"])
            out["days"][key] = clean
    books = data.get("books")
    if isinstance(books, dict):
        for k, book in books.items():
            if not isinstance(book, dict):
                continue
            row = _num_row(book)
            days_in = book.get("days") if isinstance(book.get("days"), dict) else {}
            per_day = {}
            for dk, dv in days_in.items():
                if len(str(dk)) != 10:
                    continue
                clean = _num_row(dv)
                if clean["sec"] > 0 or clean["chars"] > 0:
                    per_day[str(dk)] = clean
            out["books"][str(k)] = {
                "title": str(book.get("title") or ""),
                "sec": row["sec"], "chars": row["chars"], "n": row["n"],
                "first": max(0.0, _num(book.get("first"))),
                "last": max(0.0, _num(book.get("last"))),
                "days": per_day,
            }
    sessions = data.get("sessions")
    if isinstance(sessions, (list, tuple)):
        keep = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sec = max(0.0, _num(s.get("sec")))
            start = _num(s.get("start"))
            if sec <= 0 or start <= 0:
                continue
            keep.append({"k": str(s.get("k") or ""), "title": str(s.get("title") or ""),
                         "start": start, "sec": sec,
                         "chars": max(0.0, _num(s.get("chars"))),
                         "end": max(_num(s.get("end")), start),
                         "gaps": _gaps(s.get("gaps"))})
        keep.sort(key=lambda s: s["start"])
        out["sessions"] = keep[-KEEP_SESSIONS:]
    return out


def _reuse(data, fresh):
    """原地把 fresh 灌进调用方给的那个 dict（调用方拿着引用，不能换成新对象）。"""
    if isinstance(data, dict):
        data.clear()
        data.update(fresh)
        return data
    return fresh


def load(path):
    """读账本；读不动、不是 JSON、结构离谱 → 当空的（绝不因为统计文件起不来）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return blank()
    return normalize(raw)


def save(path, data):
    """先写 .tmp 再 os.replace，写一半断电也不会留下半个文件。"""
    clean = normalize(data)
    payload = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    tmp = path + ".tmp"
    try:
        folder = os.path.dirname(os.path.abspath(path))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------- 记账
def add_span(data, key, title, start_ts, sec, chars, end_ts=None, gaps=None):
    """把一段阅读记进日账 / 书账 / 会话表。原地改 `data`，返回它。

    `gaps` 是这段内部的空隙 `[[偏移(相对 start_ts), 秒], ...]`，只用来画时间线。
    会话这里有两道闸，都是为了让"一场阅读"看起来像一场，而不是一串碎渣：

    * **并回同一场**：同一本书、和它上一场之间断不到 `SESSION_GAP_SEC`，就并回去
      （秒数、字数相加、结束时间往后延，两场之间那段记成一个 gap）。
      换个窗口看一眼、出去接个电话回来接着读，会话表里仍然是一整场。
    * **过短不记**：接不上上一场、本身又不到 `MIN_SESSION_SEC` 的，直接不算
      （打开又关掉、随手瞟一眼那种），日账和书账也不记——"没看的会话不用记录"。

    顺序很重要：先试着并回去，并得进去就不算短（一场正常阅读的尾巴可以是很短的一截）。
    """
    data = _reuse(data, normalize(data))
    sec = max(0.0, _num(sec))
    chars = max(0.0, _num(chars))
    if sec <= 0 and chars <= 0:
        return data
    start = _num(start_ts) or time.time()
    end = _num(end_ts) or (start + sec)
    end = max(end, start)
    key = str(key or "")
    span_gaps = _gaps(gaps)
    sessions = data["sessions"]

    merged = False
    for s in reversed(sessions):                 # 只并"这本书的上一场"，中间夹别的书不影响
        if str(s.get("k") or "") != key:
            continue
        last_start = _num(s.get("start"))
        last_end = _num(s.get("end")) or last_start
        if start < last_start - 1 or (start - last_end) > SESSION_GAP_SEC:
            break                                # 断得太久 / 时间倒流：不是同一场
        shift = start - last_start
        pause = start - last_end
        if pause > IDLE_SEC:
            s.setdefault("gaps", [])
            s["gaps"] = _gaps(list(s["gaps"]) + [[max(0.0, shift - pause), pause]])
        s["sec"] = _num(s.get("sec")) + sec
        s["chars"] = _num(s.get("chars")) + chars
        s["end"] = max(last_end, end)
        if span_gaps:
            s["gaps"] = _gaps(list(s.get("gaps") or []) +
                              [[shift + off, dur] for off, dur in span_gaps])
        if title:
            s["title"] = str(title)
        merged = True
        break

    if not merged:
        if sec < MIN_SESSION_SEC:
            return data                     # 太短、又接不上：这就是"没看"，一个字都不记
        sessions.append({"k": key, "title": str(title or ""), "start": start,
                         "sec": sec, "chars": chars, "end": end,
                         "gaps": span_gaps})
        data["sessions"] = sessions[-KEEP_SESSIONS:]

    day = day_key(start)
    row = data["days"].setdefault(day, {"sec": 0.0, "chars": 0.0, "n": 0})
    row["sec"] = min(DAY_SEC_CAP, row["sec"] + sec)
    row["chars"] += chars
    if not merged:
        row["n"] += 1

    book = data["books"].setdefault(key, {"title": "", "sec": 0.0, "chars": 0.0, "n": 0,
                                          "first": 0.0, "last": 0.0, "days": {}})
    if title:
        book["title"] = str(title)
    book["sec"] += sec
    book["chars"] += chars
    if not merged:
        book["n"] += 1
    book["first"] = min(book["first"] or start, start)
    book["last"] = max(book["last"] or 0.0, end)
    sub_row = book["days"].setdefault(day, {"sec": 0.0, "chars": 0.0, "n": 0})
    sub_row["sec"] += sec
    sub_row["chars"] += chars
    if not merged:
        sub_row["n"] += 1

    data["days"] = _trim(data["days"], KEEP_DAYS)
    return data


def consolidate(data, gap_sec=None):
    """把会话表按"同一本书、断不到 `gap_sec`（默认 45 分钟）就并成一场"重排一遍。

    旧口径记下来的会话是一串碎渣（5 秒一场、50 秒一场），看着不像"读了一次书"。
    这里**只重排会话，秒数 / 字数一个字都不改**：

    * 相邻两段同书、间隔不到 `gap_sec` → 并成一场，间隔 > `IDLE_SEC` 的记成一个 gap；
    * 每段自己带的 gaps 跟着一起搬（偏移按并进来的位置平移）；
    * 老记录没记场内空隙，"这一段记了多久之外"的那段时间补成一个 gap ——
      不然时间线上画的"在读"（整场减去空隙）会比账上记的时长多出一大截；
      自己带着空隙的会话不补（它就是新的口径），所以对同一份账本反复调用结果是稳定的；
    * 日账 / 书账按并完的会话重算（`n` 是场次，所以会跟着变小）。

    返回新账本（不原地改传入的那个）。
    """
    gap_sec = SESSION_GAP_SEC if gap_sec is None else max(0.0, _num(gap_sec))
    src = normalize(data)
    rows = sorted(src["sessions"], key=lambda s: _num(s.get("start")))

    groups = []
    cur = []
    cur_end = 0.0
    for s in rows:
        start = _num(s.get("start"))
        end = max(_num(s.get("end")), start)
        same_book = bool(cur) and str(s.get("k") or "") == str(cur[-1].get("k") or "")
        if same_book and (start - cur_end) < gap_sec:
            cur.append(s)
            cur_end = max(cur_end, end)
        else:
            if cur:
                groups.append(cur)
            cur = [s]
            cur_end = end
    if cur:
        groups.append(cur)

    out = blank()
    for group in groups:
        start = min(_num(x.get("start")) for x in group)
        end = max(max(_num(x.get("end")), _num(x.get("start"))) for x in group)
        sec = sum(_num(x.get("sec")) for x in group)
        chars = sum(_num(x.get("chars")) for x in group)
        gaps = []
        prev_end = None
        for x in group:
            s0 = _num(x.get("start"))
            e0 = max(_num(x.get("end")), s0)
            shift = s0 - start
            own = _gaps(x.get("gaps"))
            if prev_end is not None and (s0 - prev_end) > IDLE_SEC:
                gaps.append([max(0.0, prev_end - start), s0 - prev_end])
            for off, dur in own:
                gaps.append([max(0.0, shift + off), dur])
            inner = (e0 - s0) - _num(x.get("sec")) - sum(d for _, d in own)
            if not own and inner > IDLE_SEC:        # 老账本没记场内空隙：补一段，时间线才对得上
                off = min(max(0.0, shift + _num(x.get("sec"))), end - start)
                inner = min(inner, max(0.0, (end - start) - off))
                if inner > IDLE_SEC:
                    gaps.append([off, inner])
            prev_end = max(prev_end or 0.0, e0)
        out["sessions"].append({
            "k": str(group[0].get("k") or ""), "title": str(group[0].get("title") or ""),
            "start": start, "sec": sec, "chars": chars, "end": end, "gaps": _gaps(gaps)})

    out["sessions"].sort(key=lambda s: s["start"])
    out["sessions"] = out["sessions"][-KEEP_SESSIONS:]
    for s in out["sessions"]:
        day = day_key(s["start"])
        row = out["days"].setdefault(day, {"sec": 0.0, "chars": 0.0, "n": 0})
        row["sec"] = min(DAY_SEC_CAP, row["sec"] + s["sec"])
        row["chars"] += s["chars"]
        row["n"] += 1
        book = out["books"].setdefault(s["k"], {"title": "", "sec": 0.0, "chars": 0.0,
                                                "n": 0, "first": 0.0, "last": 0.0, "days": {}})
        if s["title"]:
            book["title"] = s["title"]
        book["sec"] += s["sec"]
        book["chars"] += s["chars"]
        book["n"] += 1
        book["first"] = min(book["first"] or s["start"], s["start"])
        book["last"] = max(book["last"] or 0.0, s["end"])
        sub = book["days"].setdefault(day, {"sec": 0.0, "chars": 0.0, "n": 0})
        sub["sec"] += s["sec"]
        sub["chars"] += s["chars"]
        sub["n"] += 1
    return normalize(out)


def _trim(table, keep):
    """只留最近的 `keep` 个日期。"""
    if len(table) <= keep:
        return table
    for k in sorted(table)[:-keep]:
        table.pop(k, None)
    return table


# ---------------------------------------------------------------- 时间线
def session_gaps(s):
    """一场的内部空隙 `[[偏移, 秒], ...]`（老账本没有这一项时给空表）。"""
    if not isinstance(s, dict):
        return []
    return _gaps(s.get("gaps"))


def active_intervals(s):
    """这一场里"真在读"的时间段 [(起, 止), ...]：整场减去空隙。"""
    if not isinstance(s, dict):
        return []
    start = _num(s.get("start"))
    if start <= 0:
        return []
    end = max(_num(s.get("end")), start + _num(s.get("sec")))
    out = []
    cur = start
    for off, dur in session_gaps(s):
        g0 = start + off
        g1 = min(end, g0 + dur)
        if g1 <= cur or g0 >= end:
            continue
        if g0 > cur:
            out.append((cur, g0))
        cur = max(cur, g1)
        if cur >= end:
            break
    if cur < end:
        out.append((cur, end))
    return [(a, b) for a, b in out if b > a]


def day_slots(data, day=None, key="", slot_sec=SLOT_SEC):
    """某一天的时间线：每格在读的秒数（默认 5 分钟一格，288 格）。

    `key` 非空就只算那本书。格子的秒数是被会话的活跃段裁出来的，
    所以"读了 3 分钟就停"那种会显示成半格，不是整格。
    """
    day = str(day or day_key())
    base = _day_start_epoch(day)
    n = max(1, int(86400 // max(60, int(slot_sec))))
    slots = [0.0] * n
    for s in data.get("sessions") or []:
        if key and str(s.get("k") or "") != key:
            continue
        for a, b in active_intervals(s):
            if b <= base or a >= base + n * slot_sec:
                continue
            c0 = max(0, int((a - base) // slot_sec))
            c1 = min(n - 1, int((b - base - 0.001) // slot_sec))
            for c in range(c0, c1 + 1):
                seg_a = max(a, base + c * slot_sec)
                seg_b = min(b, base + (c + 1) * slot_sec)
                if seg_b > seg_a:
                    slots[c] += (seg_b - seg_a)
    return slots


def today_sessions(data, day=None, key=""):
    """这一天的会话（给时间线下面列"哪几段"用），最新的在前。"""
    day = str(day or day_key())
    out = [s for s in (data.get("sessions") or [])
           if day_key(s.get("start")) == day and (not key or str(s.get("k") or "") == key)]
    return list(reversed(out))


# ---------------------------------------------------------------- 聚合
def speed(data, key=""):
    """字/分：总字数 ÷ 总分钟。`key` 为空就是全局。"""
    if key:
        book = (data.get("books") or {}).get(str(key)) or {}
        sec, chars = _num(book.get("sec")), _num(book.get("chars"))
    else:
        sec = sum(_num(r.get("sec")) for r in (data.get("days") or {}).values())
        chars = sum(_num(r.get("chars")) for r in (data.get("days") or {}).values())
    if sec < 10:
        return 0.0
    return chars / (sec / 60.0)


def active_days(data, key=""):
    """有记录的日子（升序）。"""
    if key:
        book = (data.get("books") or {}).get(str(key)) or {}
        return sorted(k for k, v in (book.get("days") or {}).items() if _num(v.get("sec")) > 0)
    return sorted(k for k, v in (data.get("days") or {}).items() if _num(v.get("sec")) > 0)


def streak(data, key="", now=None):
    """从今天往回连着读了多少天。今天还没读就看昨天（今天没读不算断）。"""
    days = set(active_days(data, key))
    if not days:
        return 0
    today = datetime.date.fromisoformat(day_key(now))
    if today.isoformat() not in days:
        today = today - datetime.timedelta(days=1)
    n = 0
    while today.isoformat() in days:
        n += 1
        today = today - datetime.timedelta(days=1)
    return n


def series(data, days=14, key="", now=None):
    """近 N 天：[(日期, 秒), ...]，从早到晚。"""
    days = max(1, int(days))
    base = datetime.date.fromisoformat(day_key(now))
    out = []
    if key:
        book = (data.get("books") or {}).get(str(key)) or {}
        table = book.get("days") or {}
    else:
        table = data.get("days") or {}
    for i in range(days - 1, -1, -1):
        d = (base - datetime.timedelta(days=i)).isoformat()
        out.append((d, _num((table.get(d) or {}).get("sec"))))
    return out


def heatmap(data, weeks=53, key="", now=None):
    """53 周 × 7 行：[[(日期, 秒) 或 None, ...7], ...weeks]，最后一列含今天。"""
    weeks = max(1, int(weeks))
    today = datetime.date.fromisoformat(day_key(now))
    # 行 = 周一..周日；最后一列的周日就是今天所在那周的周日
    end = today + datetime.timedelta(days=(6 - today.weekday()))
    start = end - datetime.timedelta(days=weeks * 7 - 1)
    if key:
        book = (data.get("books") or {}).get(str(key)) or {}
        table = book.get("days") or {}
    else:
        table = data.get("days") or {}
    cols = []
    for w in range(weeks):
        col = []
        for d in range(7):
            day = start + datetime.timedelta(days=w * 7 + d)
            if day > today:
                col.append(None)
                continue
            k = day.isoformat()
            col.append((k, _num((table.get(k) or {}).get("sec"))))
        cols.append(col)
    return cols


def month_marks(cols):
    """热力图上要标的月份：[(列号, '11月'), ...]（跨月才标，第一列标第一个月）。"""
    marks = []
    last = None
    for i, col in enumerate(cols or []):
        first = next((cell for cell in col if cell), None)
        if not first:
            continue
        month = first[0][5:7]
        if month != last:
            marks.append((i, "%d月" % int(month)))
            last = month
    return marks


def book_view(data, key, total=0, pos=0, now=None):
    """一本书的汇总（统计页用的那一份）。"""
    key = str(key or "")
    book = (data.get("books") or {}).get(key) or {}
    days = book.get("days") or {}
    sec = _num(book.get("sec"))
    chars = _num(book.get("chars"))
    n = int(_num(book.get("n")))
    day_keys = sorted(k for k, v in days.items() if _num(v.get("sec")) > 0)
    span_days = 0
    if len(day_keys) >= 2:
        span_days = (datetime.date.fromisoformat(day_keys[-1]) -
                     datetime.date.fromisoformat(day_keys[0])).days + 1
    elif day_keys:
        span_days = 1
    total = max(0, int(_num(total)))
    pos = max(0, int(_num(pos)))
    remain = max(0, total - pos) if total else 0
    sp = speed(data, key)
    remaining_sec = 0.0
    if remain and sp > 0:
        remaining_sec = remain / sp * 60.0
    return {
        "key": key, "title": book.get("title") or "",
        "sec": sec, "chars": chars, "sessions": n, "speed": sp,
        "total": total, "pos": pos,
        "today_sec": _num(((book.get("days") or {}).get(day_key(now)) or {}).get("sec")),
        "pct": (100.0 * pos / total) if total else 0.0,
        "remain": remain, "remaining_sec": remaining_sec,
        "first": _num(book.get("first")), "last": _num(book.get("last")),
        "active_days": len(day_keys), "span_days": span_days,
        "avg_session": (sec / n) if n else 0.0,
        "streak": streak(data, key, now),
    }


# ---------------------------------------------------------------- 窗口（今天 / 本周 / 本月 / 全部）
RANGES = (("today", "今天"), ("week", "本周"), ("month", "本月"), ("all", "全部"))
WEEKDAY_CN = "一二三四五六日"


def _today(now=None):
    return datetime.date.fromisoformat(day_key(now))


def _week_start(d):
    return d - datetime.timedelta(days=d.weekday())


def range_bounds(name, now=None):
    """窗口 → (起 date, 止 date)。'全部' 靠数据定，返回 (None, None)。"""
    today = _today(now)
    if name == "week":
        start = _week_start(today)
        return start, start + datetime.timedelta(days=6)
    if name == "month":
        first = today.replace(day=1)
        nxt = (first + datetime.timedelta(days=32)).replace(day=1)
        return first, nxt - datetime.timedelta(days=1)
    if name == "all":
        return None, None
    return today, today


def prev_bounds(name, now=None):
    """上一个同类窗口（今天→昨天 / 本周→上周 / 本月→上月）；'全部' 没有上一期。"""
    today = _today(now)
    if name == "week":
        start = _week_start(today) - datetime.timedelta(days=7)
        return start, start + datetime.timedelta(days=6)
    if name == "month":
        this_first = today.replace(day=1)
        last_end = this_first - datetime.timedelta(days=1)
        return last_end.replace(day=1), last_end
    if name == "all":
        return None, None
    y = today - datetime.timedelta(days=1)
    return y, y


def range_label(name, now=None):
    """给人看的一行：'今天 · 11-10 周一' / '本周 · 11-10 → 11-16' / '全部 · 10-01 起'。"""
    today = _today(now)
    start, end = range_bounds(name, now)
    if name == "today":
        return "今天 · %s 周%s" % (today.strftime("%m-%d"), WEEKDAY_CN[today.weekday()])
    if name == "all":
        return "全部"
    return "%s · %s → %s" % (dict(RANGES)[name], start.strftime("%m-%d"), end.strftime("%m-%d"))


def range_days(data, name, key="", now=None):
    """窗口内有记录的日子（升序，只到数据里有的）。"""
    start, end = range_bounds(name, now)
    days = active_days(data, key)
    if start is None:
        return days
    lo, hi = start.isoformat(), end.isoformat()
    return [d for d in days if lo <= d <= hi]


def _bucket_for(name, days, now=None):
    """自动粒度：今天=5 分钟带；一周 / 一个月=按天；更长的=按月。"""
    if name == "today":
        return "slot"
    if name == "all" and len(days) >= 2:
        span = (datetime.date.fromisoformat(days[-1]) -
                datetime.date.fromisoformat(days[0])).days + 1
        if span > 62:
            return "month"
    return "day"


def range_points(data, name="today", key="", now=None):
    """图上的点。返回 {"kind", "slots"/"points", "caption"}。

    kind='slot'  → slots 是 288 格（5 分钟一格），今天专用；
    kind='bars'  → points 是 [(短标签, 秒, 原始键), ...]，按天或按月（原始键=那天的 ISO 或那个月）。
    """
    days = range_days(data, name, key, now)
    bucket = _bucket_for(name, days, now)
    if bucket == "slot":
        return {"kind": "slot",
                "slots": day_slots(data, day_key(now), key),
                "caption": "今天 · 5 分钟一格"}
    table = {}
    if key:
        table = ((data.get("books") or {}).get(str(key)) or {}).get("days") or {}
    else:
        table = data.get("days") or {}
    if bucket == "day":
        start, end = range_bounds(name, now)
        if start is None:                     # 全部但跨度小：就按有记录的那几天连着画
            if not days:
                return {"kind": "bars", "points": [], "caption": "全部 · 还没有记录"}
            start = datetime.date.fromisoformat(days[0])
            end = datetime.date.fromisoformat(days[-1])
        today = _today(now)
        labels, points = [], []
        d = start
        while d <= end:
            sec = _num((table.get(d.isoformat()) or {}).get("sec"))
            if name == "week":
                label = "周" + WEEKDAY_CN[d.weekday()]
            elif end == start:
                label = d.strftime("%m-%d")
            else:
                label = "%d" % d.day
            labels.append(label)
            points.append((label, sec, d))
            d += datetime.timedelta(days=1)
            if end > today and d > today and name != "week":
                break
        return {"kind": "bars", "points": points,
                "caption": "%s · 按天" % dict(RANGES).get(name, name)}
    # 按月
    months = []
    for dk, row in sorted(table.items()):
        if len(dk) != 10:
            continue
        if days and not (days[0] <= dk <= days[-1]):
            continue
        months.append((dk[:7], _num(row.get("sec"))))
    agg = {}
    order = []
    for mk, sec in months:
        if mk not in agg:
            agg[mk] = 0.0
            order.append(mk)
        agg[mk] += sec
    points = []
    for mk in order:
        y, m = mk.split("-")
        label = "%d月" % int(m) if len(set(k[:4] for k in order)) == 1 else "%s-%s" % (y[2:], m)
        points.append((label, agg[mk], mk))
    return {"kind": "bars", "points": points, "caption": "全部 · 按月"}


def range_stats(data, name="today", key="", now=None):
    """窗口里的一把数：时长 / 字数 / 速度 / 场次 / 平均每场 / 最好的一天 / 对比上一期。"""
    start, end = range_bounds(name, now)
    lo = start.isoformat() if start else ""
    hi = end.isoformat() if end else "9999-12-31"
    if key:
        table = ((data.get("books") or {}).get(str(key)) or {}).get("days") or {}
    else:
        table = data.get("days") or {}
    sec = chars = 0.0
    best_day, best_sec = "", 0.0
    n_days = 0
    for dk, row in table.items():
        if not (lo <= dk <= hi):
            continue
        s = _num(row.get("sec"))
        if s <= 0:
            continue
        sec += s
        chars += _num(row.get("chars"))
        n_days += 1
        if s > best_sec:
            best_day, best_sec = dk, s
    sessions = [s for s in (data.get("sessions") or [])
                if (not key or str(s.get("k") or "") == str(key))
                and lo <= day_key(s.get("start")) <= hi]
    p_start, p_end = prev_bounds(name, now)
    prev_sec = 0.0
    if p_start is not None:
        plo, phi = p_start.isoformat(), p_end.isoformat()
        prev_sec = sum(_num(r.get("sec")) for dk, r in table.items() if plo <= dk <= phi)
    delta = None
    if p_start is not None and prev_sec > 0:
        delta = (sec - prev_sec) / prev_sec * 100.0
    return {
        "name": name, "label": range_label(name, now),
        "sec": sec, "chars": chars,
        "speed": (chars / (sec / 60.0)) if sec >= 10 else 0.0,
        "sessions": len(sessions),
        "avg_session": (sec / len(sessions)) if sessions else 0.0,
        "days": n_days, "best_day": best_day, "best_sec": best_sec,
        "prev_sec": prev_sec, "delta": delta,
        "first_day": (range_days(data, name, key, now) or [""])[0],
        "last_day": (range_days(data, name, key, now) or [""])[-1],
    }


def group_sessions(data, name="all", key="", now=None):
    """窗口里的会话，按"天"分组，最新的天在前：[(日期, 周几, [会话...]), ...]。"""
    start, end = range_bounds(name, now)
    lo = start.isoformat() if start else ""
    hi = end.isoformat() if end else "9999-12-31"
    buckets = {}
    for s in (data.get("sessions") or []):
        if key and str(s.get("k") or "") != str(key):
            continue
        dk = day_key(s.get("start"))
        if not (lo <= dk <= hi):
            continue
        buckets.setdefault(dk, []).append(s)
    out = []
    for dk in sorted(buckets, reverse=True):
        items = sorted(buckets[dk], key=lambda s: _num(s.get("start")), reverse=True)
        try:
            wd = "周" + WEEKDAY_CN[datetime.date.fromisoformat(dk).weekday()]
        except ValueError:
            wd = ""
        out.append((dk, wd, items))
    return out


def books_in_range(data, name="all", now=None):
    """窗口里每本书的账，按窗口时长排序；share 是占窗口总量的比例。"""
    start, end = range_bounds(name, now)
    lo = start.isoformat() if start else ""
    hi = end.isoformat() if end else "9999-12-31"
    rows = []
    for key, book in (data.get("books") or {}).items():
        sec = chars = 0.0
        days = 0
        for dk, row in (book.get("days") or {}).items():
            if not (lo <= dk <= hi):
                continue
            s = _num(row.get("sec"))
            if s <= 0:
                continue
            sec += s
            chars += _num(row.get("chars"))
            days += 1
        if sec <= 0 and chars <= 0:
            continue
        rows.append({"key": key, "title": book.get("title") or key,
                     "sec": sec, "chars": chars, "days": days,
                     "speed": (chars / (sec / 60.0)) if sec >= 10 else 0.0,
                     "total_sec": _num(book.get("sec")),
                     "last": _num(book.get("last"))})
    total = sum(r["sec"] for r in rows)
    for r in rows:
        r["share"] = (r["sec"] / total * 100.0) if total > 0 else 0.0
    rows.sort(key=lambda r: -r["sec"])
    return rows


def fmt_delta(pct):
    """对比上一期：'↑ 23%' / '↓ 12%' / '持平' / '--'。"""
    if pct is None:
        return "--"
    v = _num(pct)
    if abs(v) < 0.5:
        return "持平"
    return "%s %.0f%%" % ("↑" if v > 0 else "↓", abs(v))


def overview(data, now=None):
    """全局概览。"""
    days = data.get("days") or {}
    today = day_key(now)
    base = datetime.date.fromisoformat(today)
    week = [(base - datetime.timedelta(days=i)).isoformat() for i in range(7)]
    sec = sum(_num(r.get("sec")) for r in days.values())
    chars = sum(_num(r.get("chars")) for r in days.values())
    sessions = data.get("sessions") or []
    return {
        "today_sec": _num((days.get(today) or {}).get("sec")),
        "today_chars": _num((days.get(today) or {}).get("chars")),
        "week_sec": sum(_num((days.get(d) or {}).get("sec")) for d in week),
        "week_chars": sum(_num((days.get(d) or {}).get("chars")) for d in week),
        "sec": sec, "chars": chars,
        "speed": (chars / (sec / 60.0)) if sec >= 10 else 0.0,
        "sessions": len(sessions),
        "avg_session": (sum(_num(s.get("sec")) for s in sessions) / len(sessions)) if sessions else 0.0,
        "streak": streak(data, "", now),
        "active_days": len(active_days(data)),
        "days": days,
        "first_day": min(days) if days else "",
        "last_day": max(days) if days else "",
    }
