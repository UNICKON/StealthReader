#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阅读数据的云端同步（标准库，不碰 Qt）。

设计见 `docs/cloudflare-sync-design.md`。这里只放"能单测的纯函数 + 一个后台线程 + 一个 HTTP 客户端"，
界面和写回的事都在 `main.py`：本模块算出"合并之后的本地形状数据"，交给主线程去写进内存对象再存盘。

三条硬规矩（改代码前先读这三条）：

1. **合并只并集、只取新，永不删**（删除只认墓碑）。宁可多留一条笔记，也不许少一条。
2. **账本不能越并越大**：同一场（同书、同 start）的新旧快照只保留 end 更晚的那份，绝不求和；
   合并后 sec 不许超过墙钟跨度（见 `merge_stats`）。
3. **运行时状态只写 `sync_state.json`**，一个字都不往 `config.json` 里塞——那份文件每翻一页
   就被应用用内存副本整份重写一次，塞进去会被擦掉。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse

try:
    import reading_stats as rs
except Exception:                                    # 单独跑自检时也能用
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import reading_stats as rs

# ---------------------------------------------------------------- 常量

STATE_VERSION = 1
EXTRACT_SCHEMA = 1              # 正文抽取管线的版本。抽取规则改了就 +1，免得两台机器的偏移对不上
STATE_FILE = "sync_state.json"
MIN_SESSION_SEC = 60            # 和 reading_stats 一致
SEC_SLACK = 5.0                 # 一拍的长度：sec 比 end-start 天然多一拍，截断时要留出来
HTTP_TIMEOUT = 45.0             # 单次请求最长 45 秒（8 MB 的书在慢链路上要十几秒，留足余量）
MAX_CONFLICT_RETRY = 3
SYNC_DEBUG = os.environ.get("STEALTHREADER_SYNC_DEBUG") == "1"   # 逐 kind 的细节日志
KEEP_VERSIONS = 5

SYNC_KINDS = ("progress", "annotations", "stats", "settings")

# 这些键跨机器有意义；其余（窗口大小/位置/上一次打开的书/safe_doc_path 这类）是设备本地的，别同步
SYNC_SETTING_KEYS = (
    "font_size", "font_family", "line_spacing", "paragraph_spacing", "page_margin_chars",
    "first_line_indent", "paragraph_indent", "page_overlap_lines", "arrow_scroll_lines",
    "text_color", "bg_color", "opacity", "lowkey_key", "peek_key", "boss_key", "solid_key",
    "safe_key", "anki_deck", "anki_swallow_keys", "anki_conceal", "feed_lowkey",
    "feed_type_scheme", "auto_bg_solid", "ghost_mode", "antishot_mode", "restart_on_unlock",
)


def _num(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _now():
    return time.time()


def _iso(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_num(ts)))
    except Exception:
        return ""


# ---------------------------------------------------------------- 文件读写（原子 + 锁）

def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def write_json_atomic(path, data, keep_bak=2):
    """先写临时文件再替换；替换前把旧文件留成 .bak（滚动 keep_bak 份）。"""
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    if keep_bak and os.path.exists(path):
        try:
            for i in range(keep_bak - 1, 0, -1):
                older = "%s.bak%d" % (path, i)
                newer = "%s.bak%d" % (path, i + 1)
                if os.path.exists(newer):
                    os.replace(newer, older)
                elif i == keep_bak - 1 and os.path.exists(older):
                    os.remove(older)
            if os.path.exists(path + ".bak"):
                os.replace(path + ".bak", path + ".bak2")
            with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                dst.write(src.read())
        except Exception:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    # Windows 上 os.replace 偶尔被"别人正好开着这个文件"（杀软/索引/探针）挡一下，
    # 重试几次基本就过去了；真不行就直接写——宁可少一层原子性，也别把缓存丢了
    # （缓存丢了下一轮就会把没变的 kind 全下载一遍）。
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                break
            time.sleep(0.05 * (attempt + 1))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    try:
        os.remove(tmp)
    except OSError:
        pass


class FileLock:
    """跨进程互斥（同一台机器上探针/应用同时跑时有用）。拿不到就跳过这轮，不排队等。"""

    def __init__(self, path, stale_sec=60):
        self.path = path
        self.stale_sec = stale_sec
        self.fd = None

    def __enter__(self):
        try:
            if os.path.exists(self.path) and _now() - os.path.getmtime(self.path) > self.stale_sec:
                os.remove(self.path)
        except Exception:
            pass
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode("ascii"))
            return True
        except FileExistsError:
            return False

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
        except Exception:
            pass
        self.fd = None
        try:
            os.remove(self.path)
        except Exception:
            pass
        return False

    def __bool__(self):
        return self.fd is not None


# ---------------------------------------------------------------- 书身份（路径 → 内容 UID）

def _hash_file(path, limit_bytes=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            if limit_bytes is not None and f.tell() >= limit_bytes:
                break
    return h.hexdigest()


def synthetic_uid(path_key):
    """算不出内容 UID 的书（文件已经删了）用这个，只为了"笔记别丢"。"""
    norm = os.path.normcase(os.path.abspath(str(path_key))).encode("utf-8", "replace")
    return "x" + hashlib.sha1(norm).hexdigest()[:15]


def uid_for(path_key, cache):
    """返回 (uid, info)。info 里 size/mtime 用于缓存校验；`stale` 表示文件已经不在了。"""
    key = str(path_key or "")
    if not key:
        return "", {}
    hit = cache.get(key)
    try:
        st = os.stat(key)
    except OSError:
        uid = synthetic_uid(key)
        info = {"uid": uid, "stale": True}
        cache[key] = info
        return uid, info
    if (isinstance(hit, dict) and not hit.get("stale")
            and int(hit.get("size", -1)) == st.st_size
            and abs(_num(hit.get("mtime")) - st.st_mtime) < 1e-6):
        return str(hit.get("uid") or ""), hit
    try:
        uid = _hash_file(key)[:16]
    except OSError:
        uid = synthetic_uid(key)
        info = {"uid": uid, "stale": True}
        cache[key] = info
        return uid, info
    info = {"uid": uid, "size": st.st_size, "mtime": st.st_mtime}
    cache[key] = info
    return uid, info


def uid_map_for(config, cache):
    """整本 config 里出现过的书（进度表 + 书架时间表）→ {path_key: uid}。"""
    out = {}
    tables = []
    for name in ("local_book_positions", "local_book_totals", "local_book_times"):
        t = config.get(name)
        if isinstance(t, dict):
            tables.append(t)
    hidden = config.get("local_book_hidden")
    if isinstance(hidden, list):
        tables.append({k: 1 for k in hidden})
    for table in tables:
        for path in table:
            uid, info = uid_for(path, cache)
            if uid:
                out[str(path)] = uid
    return out


def uid_to_paths(uidmap):
    out = {}
    for path, uid in uidmap.items():
        out.setdefault(uid, []).append(path)
    return out


# ---------------------------------------------------------------- 本地 → 同步对象（"视图"）

def progress_view(config, uidmap, device, cache=None):
    pos = config.get("local_book_positions") or {}
    totals = config.get("local_book_totals") or {}
    times = config.get("local_book_times") or {}
    hidden = set(config.get("local_book_hidden") or [])
    per_uid = {}
    for path, uid in uidmap.items():
        if not uid:
            continue
        row = per_uid.setdefault(uid, {"title": os.path.basename(path), "pos": 0, "total": 0,
                                       "at": 0.0, "dev": device, "hidden": False})
        row["pos"] = max(int(_num(row.get("pos"))), int(_num(pos.get(path))))
        row["total"] = max(int(_num(row.get("total"))), int(_num(totals.get(path))))
        row["at"] = max(_num(row.get("at")), _num(times.get(path)))
        row["hidden"] = bool(row.get("hidden")) or (path in hidden)
        if cache is not None and (cache.get(path) or {}).get("stale"):
            row["stale"] = True
    return {"v": STATE_VERSION, "schema": EXTRACT_SCHEMA, "updated_at": _now(),
            "device": device, "books": per_uid}


def _item_at(item):
    """划线/书签的时间：优先 `at`，其次从 id 里的毫秒数抠（`h1762949000123`）。"""
    at = _num((item or {}).get("at"))
    if at > 0:
        return at
    mid = str((item or {}).get("id") or "")
    m = re.match(r"^[a-z]?(\d{11,16})$", mid)
    if m:
        return _num(m.group(1)) / 1000.0
    return 0.0


def annotations_view(annotations, uidmap, device, tombstones=None, cache=None):
    """annotations.json 是 {path: {highlights, bookmarks}}，这里按 UID 归并。

    `uidmap` 只包含"书架上有进度记录"的书；笔记/划线可能出现在只翻了两页就关掉、
    或者文件已经被挪走的书上——那种书也要有自己的身份（`uid_for` 会给合成 UID），
    否则它的笔记永远上不去（这台机器 9 条划线只传上去 3 条就是这么来的）。
    """
    path_to_uid = uidmap
    per_uid = {}
    for path, store in (annotations or {}).items():
        if not isinstance(store, dict):
            continue
        uid = path_to_uid.get(str(path))
        if not uid and cache is not None:
            uid, _info = uid_for(str(path), cache)
        if not uid:
            continue
        row = per_uid.setdefault(uid, {"highlights": [], "bookmarks": [], "tombstones": []})
        for kind in ("highlights", "bookmarks"):
            for item in store.get(kind) or []:
                if not isinstance(item, dict):
                    continue
                item = dict(item)
                item["at"] = _item_at(item)
                row[kind].append(item)
    for uid, row in per_uid.items():
        for tid, at in ((tombstones or {}).get(uid) or {}).items():
            row["tombstones"].append({"id": tid, "at": _num(at)})
    return {"v": STATE_VERSION, "schema": EXTRACT_SCHEMA, "updated_at": _now(),
            "device": device, "books": per_uid}


def stats_view(stats, uidmap, device):
    """账本：会话的 k 换成 UID（换不出来的保留原键），days/books 一起带上（见设计 §7.2）。"""
    src = rs.normalize(stats or {})
    path_to_uid = uidmap
    sessions = []
    for s in src.get("sessions") or []:
        row = dict(s)
        old_key = str(row.get("k") or "")
        row["k"] = path_to_uid.get(old_key, old_key)
        sessions.append(row)
    books = {}
    for key, book in (src.get("books") or {}).items():
        if not isinstance(book, dict):
            continue
        row = dict(book)
        row["k"] = path_to_uid.get(str(key), str(key))
        books[str(key) if str(key) not in path_to_uid else path_to_uid[str(key)]] = row
    return {"v": STATE_VERSION, "updated_at": _now(), "device": device,
            "sessions": sessions, "days": dict(src.get("days") or {}), "books": books}


def settings_view(config, clock, device):
    values = {}
    for key in SYNC_SETTING_KEYS:
        if key in config:
            values[key] = config[key]
    stamps = {}
    for key in values:
        stamps[key] = _num((clock or {}).get(key))
    return {"v": STATE_VERSION, "updated_at": _now(), "device": device,
            "values": values, "clock": stamps}


# ---------------------------------------------------------------- 合并（纯函数，逐条对应设计 §7）

def _later(a, b):
    """两个"条目"谁更新：比 at，at 相同再比内容长度，保证对称（merge(a,b) == merge(b,a)）。"""
    if _num(a.get("at")) != _num(b.get("at")):
        return a if _num(a.get("at")) > _num(b.get("at")) else b
    da = json.dumps(a, ensure_ascii=False, sort_keys=True)
    db = json.dumps(b, ensure_ascii=False, sort_keys=True)
    return a if da >= db else b


def _view_hash(view):
    """内容指纹：只看"内容"，不算 updated_at / device / rev 这些每轮都变的字段。

    用来回答"这次合并的结果跟远端那份是不是一模一样"——一样就别写（R2 每次写都换 rev，
    另一台机器看见 rev 变了又会白跑一轮）。
    """
    if not isinstance(view, dict):
        return ""
    clean = {k: v for k, v in view.items() if k not in ("updated_at", "device", "rev")}
    blob = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _diff_brief(a, b, limit=8):
    """两份视图差在哪（只给调试日志用）：顶层键，dict 再下钻一层，最多列 limit 条。"""
    out = []

    def walk(x, y, path):
        if len(out) >= limit:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                if len(out) >= limit:
                    return
                p = "%s.%s" % (path, k) if path else str(k)
                if k not in x:
                    out.append(p + " 只在远端")
                elif k not in y:
                    out.append(p + " 只在本机")
                elif x[k] != y[k]:
                    if isinstance(x[k], dict) and isinstance(y[k], dict):
                        walk(x[k], y[k], p)
                    else:
                        out.append("%s：%r ≠ %r" % (p, _short(x[k]), _short(y[k])))
    if isinstance(a, dict) and isinstance(b, dict):
        walk(a, b, "")
    return out


def _short(v):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= 60 else s[:57] + "…"


def merge_progress(local, remote):
    """进度：同 UID 取 at 更晚的；at 相同取 pos 更大；total 取更大；hidden 取或。

    每一行都过 `_progress_row()` 收成同一个形状（七个字段齐全），否则"本地独有"的分支
    和"两边都有"的分支输出会差一个键，合并就不幂等了。
    """
    local = local if isinstance(local, dict) else {}
    remote = remote if isinstance(remote, dict) else {}
    out = {"v": STATE_VERSION, "schema": EXTRACT_SCHEMA,
           "updated_at": max(_num(local.get("updated_at")), _num(remote.get("updated_at"))),
           "device": local.get("device") or remote.get("device") or "", "books": {}}
    lb = local.get("books") if isinstance(local.get("books"), dict) else {}
    rb = remote.get("books") if isinstance(remote.get("books"), dict) else {}

    def row_of(uid):
        a = lb.get(uid) if isinstance(lb.get(uid), dict) else {}
        b = rb.get(uid) if isinstance(rb.get(uid), dict) else {}
        at_a, at_b = _num(a.get("at")), _num(b.get("at"))
        if at_a > at_b:
            win = a
        elif at_b > at_a:
            win = b
        else:
            win = a if _num(a.get("pos")) >= _num(b.get("pos")) else b
        return {"title": win.get("title") or a.get("title") or b.get("title") or "",
                "pos": int(_num(win.get("pos"))) if at_a != at_b else max(int(_num(a.get("pos"))), int(_num(b.get("pos")))),
                "total": max(int(_num(a.get("total"))), int(_num(b.get("total")))),
                "at": max(at_a, at_b),
                "dev": win.get("dev") or a.get("dev") or b.get("dev") or "",
                "hidden": bool(a.get("hidden")) or bool(b.get("hidden")),
                "stale": bool(a.get("stale")) or bool(b.get("stale"))}

    for uid in set(lb) | set(rb):
        out["books"][uid] = row_of(uid)
    # schema 取更高的一边：低的偏移解释不了高的
    out["schema"] = max(int(_num(local.get("schema")) or EXTRACT_SCHEMA),
                        int(_num(remote.get("schema")) or EXTRACT_SCHEMA))
    return out


def merge_annotations(local, remote):
    """划线/书签：按 id 并集，同 id 取 at 更晚；墓碑并集后生效（墓碑比条目新才删）。"""
    local = local if isinstance(local, dict) else {}
    remote = remote if isinstance(remote, dict) else {}
    out = {"v": STATE_VERSION,
           "schema": max(int(_num(local.get("schema")) or EXTRACT_SCHEMA),
                         int(_num(remote.get("schema")) or EXTRACT_SCHEMA)),
           "updated_at": max(_num(local.get("updated_at")), _num(remote.get("updated_at"))),
           "device": local.get("device") or remote.get("device") or "", "books": {}}
    lb = local.get("books") if isinstance(local.get("books"), dict) else {}
    rb = remote.get("books") if isinstance(remote.get("books"), dict) else {}

    for uid in set(lb) | set(rb):
        a = lb.get(uid) if isinstance(lb.get(uid), dict) else {}
        b = rb.get(uid) if isinstance(rb.get(uid), dict) else {}
        row = {"highlights": [], "bookmarks": [], "tombstones": []}
        tombs = {}
        for side in (a, b):
            for t in side.get("tombstones") or []:
                if not isinstance(t, dict) or not t.get("id"):
                    continue
                tid = str(t.get("id"))
                tombs[tid] = max(_num(tombs.get(tid)), _num(t.get("at")))
        for kind in ("highlights", "bookmarks"):
            by_id = {}
            for side in (a, b):
                for item in side.get(kind) or []:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    iid = str(item.get("id"))
                    cur = by_id.get(iid)
                    if cur is None:
                        by_id[iid] = dict(item)
                        by_id[iid]["at"] = _item_at(item)
                    else:
                        winner = _later(cur, item)
                        merged = dict(cur)
                        for f, v in item.items():          # 字段逐个并：远端没带的本地字段留着
                            if merged.get(f) in (None, "", 0, []) and v not in (None, "", 0, []):
                                merged[f] = v
                        merged["at"] = max(_num(cur.get("at")), _item_at(item))
                        if winner is item:
                            for f, v in item.items():
                                if v not in (None, ""):
                                    merged[f] = v
                        by_id[iid] = merged
            for iid, item in by_id.items():
                if _num(tombs.get(iid)) >= _num(item.get("at")) > 0 or (_num(tombs.get(iid)) > 0 and _num(item.get("at")) == 0):
                    continue                                # 墓碑更新或条目不晓得多老 → 删掉
                row[kind].append(item)
            row[kind].sort(key=lambda it: (int(_num(it.get("pos"))), int(_num(it.get("start"))), str(it.get("id"))))
        row["tombstones"] = [{"id": k, "at": v} for k, v in sorted(tombs.items())]
        out["books"][uid] = row
    return out


def _session_identity(s):
    return (str(s.get("k") or ""), round(_num(s.get("start")), 3))


def merge_stats(local, remote):
    """账本：同 (书, start) 只留 end 更晚的那份（不求和）；并集后 consolidate；sec 截到墙钟跨度；
    days/books 取 max(重算, 本侧存值, 对侧存值)。

    返回 (账本, 说明列表)。说明列表里放"截断了多少"这种给人看的字，主线程记日志用。
    """
    local = local if isinstance(local, dict) else {}
    remote = remote if isinstance(remote, dict) else {}
    notes = []

    # 1) 会话并集（身份相同的取 end 更晚 / sec 更大）
    chosen = {}
    for src in (local, remote):
        for s in src.get("sessions") or []:
            if not isinstance(s, dict):
                continue
            ident = _session_identity(s)
            if not ident[0] or ident[1] <= 0 or _num(s.get("sec")) <= 0 and _num(s.get("chars")) <= 0:
                continue
            cur = chosen.get(ident)
            if cur is None:
                chosen[ident] = dict(s)
            else:
                cur_end = _num(cur.get("end"))
                new_end = _num(s.get("end"))
                if new_end > cur_end or (new_end == cur_end and _num(s.get("sec")) > _num(cur.get("sec"))):
                    chosen[ident] = dict(s)

    merged = {"sessions": list(chosen.values()),
              "days": {}, "books": {}}
    # 2) 先 consolidate（同书 15 分钟内并成一场、求和；日账/书账它自己会重算）
    try:
        merged = rs.consolidate(merged)
    except Exception as e:
        notes.append("consolidate 失败：%s" % e)
        merged = {"sessions": list(chosen.values()), "days": {}, "books": {}}

    # 3) sec 截到墙钟跨度（正常记录本来就满足；截断说明两边重叠了）
    #    留一拍（5 秒）的余量：`sec` 是"数了几拍"，`end - start` 是"第一个点到最后一个点"，
    #    最后一拍的 5 秒落在 end 之后，所以正常记录就会大出一拍。不留余量会把真账削掉 5 秒。
    for s in merged.get("sessions") or []:
        start, sec, end = _num(s.get("start")), _num(s.get("sec")), _num(s.get("end"))
        span = end - start
        if span > 0 and sec > span + SEC_SLACK + 0.5:
            notes.append("截断：%s %s 场 %.0f→%.0f 秒" % (s.get("title") or s.get("k") or "?",
                                                    _iso(start)[11:16], sec, span + SEC_SLACK))
            s["sec"] = round(span + SEC_SLACK, 1)

    # 4) days / books：max(重算, 本地存, 远端存)
    #    注意 `books[k]` 里 first/last 是**时间戳（float）**，n/sec/chars 是数；每本书还带自己的 days。
    def bump_row(cur, row):
        for f in ("n", "sec", "chars"):
            cur[f] = max(_num(cur.get(f)), _num(row.get(f)))
        cur["n"] = int(cur["n"])
        if row.get("title") and not cur.get("title"):
            cur["title"] = str(row["title"])
        for f, pick in (("first", min), ("last", max)):
            vals = [v for v in (_num(cur.get(f)), _num(row.get(f))) if v > 0]
            if vals:
                cur[f] = pick(vals)
        sub = row.get("days")
        if isinstance(sub, dict):
            days_cur = cur.get("days") if isinstance(cur.get("days"), dict) else {}
            for dk, dv in sub.items():
                if len(str(dk)) != 10 or not isinstance(dv, dict):
                    continue
                cell = days_cur.get(str(dk)) if isinstance(days_cur.get(str(dk)), dict) else {"n": 0, "sec": 0.0, "chars": 0.0}
                for f in ("n", "sec", "chars"):
                    cell[f] = max(_num(cell.get(f)), _num(dv.get(f)))
                cell["n"] = int(cell["n"])
                days_cur[str(dk)] = cell
            if days_cur:
                cur["days"] = days_cur

    def bump(table, key, row):
        if not isinstance(row, dict):
            return
        cur = table.get(key)
        if not isinstance(cur, dict):
            cur = {"n": 0, "sec": 0.0, "chars": 0.0, "title": "", "first": 0.0, "last": 0.0, "days": {}}
            table[key] = cur
        bump_row(cur, row)

    days = {}
    for src in (local, remote):
        for key, row in (src.get("days") or {}).items():
            if len(str(key)) == 10 and isinstance(row, dict):
                bump(days, str(key), row)
    for key, row in (merged.get("days") or {}).items():          # 重算值最后并，保证"只增不减"
        if isinstance(row, dict):
            bump(days, str(key), row)

    books = {}
    for src in (local, remote):
        for key, row in (src.get("books") or {}).items():
            if isinstance(row, dict):
                bump(books, str(key), row)
    for key, row in (merged.get("books") or {}).items():
        if isinstance(row, dict):
            bump(books, str(key), row)

    out = {"sessions": sorted(merged.get("sessions") or [], key=lambda s: _num(s.get("start"))),
           "days": days, "books": books}
    return rs.normalize(out), notes


def merge_settings(local, remote, now=None):
    """设置：逐键比时钟；**时钟相同让远端赢**（否则两边"从没改过"的值永远收敛不到一起）。

    为什么不是"相同留本地"：第一次同步时两台机器都没有时钟（都是哨兵值 1），留本地就永远不传播。
    真改过的键时钟是"现在"，一定大于对方的哨兵值，所以不会因此被旧值盖掉。

    返回 `(合并后的视图, 说明)`——视图长成同步对象的样子（`values` + `clock`），
    这样调用方不用为 settings 走一条特殊路径。
    """
    now = _now() if now is None else now
    local = local if isinstance(local, dict) else {}
    remote = remote if isinstance(remote, dict) else {}
    lv = local.get("values") if isinstance(local.get("values"), dict) else {}
    rv = remote.get("values") if isinstance(remote.get("values"), dict) else {}
    lc = local.get("clock") if isinstance(local.get("clock"), dict) else {}
    rc = remote.get("clock") if isinstance(remote.get("clock"), dict) else {}
    values, clock, notes = {}, {}, []
    for key in SYNC_SETTING_KEYS:
        if key not in lv and key not in rv:
            continue
        lt, rt = _num(lc.get(key)), _num(rc.get(key))
        if key in lv and (key not in rv or lt > rt):
            values[key] = lv[key]
            clock[key] = lt
        else:
            values[key] = rv[key]
            clock[key] = rt
            if key in lv and lt < rt:
                notes.append("设置 %s 用远端的值" % key)
    return ({"v": STATE_VERSION,
             "updated_at": max(_num(local.get("updated_at")), _num(remote.get("updated_at")), now),
             "device": local.get("device") or remote.get("device") or "",
             "values": values, "clock": clock}, notes)


# ---------------------------------------------------------------- 同步对象 → 本地形状（写回用）

def progress_to_local(view, uidmap, paths_by_uid, config):
    """把合并后的进度视图摊回本机那几个表（一个 UID 写回它所有的本机路径）。"""
    pos = dict(config.get("local_book_positions") or {})
    totals = dict(config.get("local_book_totals") or {})
    times = dict(config.get("local_book_times") or {})
    hidden = list(config.get("local_book_hidden") or [])
    schema_ok = int(_num(view.get("schema")) or EXTRACT_SCHEMA) == EXTRACT_SCHEMA
    for uid, row in (view.get("books") or {}).items():
        if not isinstance(row, dict):
            continue
        for path in paths_by_uid.get(uid, []):
            if schema_ok:
                pos[path] = int(_num(row.get("pos")))
            if int(_num(row.get("total"))) > int(_num(totals.get(path))):
                totals[path] = int(_num(row.get("total")))
            if _num(row.get("at")) > _num(times.get(path)):
                times[path] = _num(row.get("at"))
            if row.get("hidden") and path not in hidden:
                hidden.append(path)
    return {"local_book_positions": pos, "local_book_totals": totals,
            "local_book_times": times, "local_book_hidden": hidden}


def annotations_to_local(view, uidmap, paths_by_uid, annotations, schema_ok=True):
    """把合并后的划线摊回本机每一条路径；本机独有的字段（color/chapter/created）保留。"""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in (annotations or {}).items()}
    for uid, row in (view.get("books") or {}).items():
        if not isinstance(row, dict):
            continue
        for path in paths_by_uid.get(uid, []):
            store = out.get(path)
            if not isinstance(store, dict):
                store = {"highlights": [], "bookmarks": []}
            for kind in ("highlights", "bookmarks"):
                existing = {str(it.get("id")): it for it in (store.get(kind) or []) if isinstance(it, dict)}
                want = []
                for item in row.get(kind) or []:
                    if not isinstance(item, dict):
                        continue
                    iid = str(item.get("id") or "")
                    old = existing.get(iid)
                    fresh = dict(item)
                    if isinstance(old, dict):                      # 本机字段优先保住
                        for f in ("color", "chapter", "created", "note", "snippet", "text"):
                            if fresh.get(f) in (None, "") and old.get(f) not in (None, ""):
                                fresh[f] = old[f]
                    fresh.pop("at", None)
                    if not schema_ok:
                        fresh.pop("start", None)
                        fresh.pop("end", None)
                    want.append(fresh)
                if want or store.get(kind):
                    store[kind] = want
            out[path] = store
    return out


def stats_to_local(view, uidmap, paths_by_uid):
    """把 UID 形状的账本翻回本机路径。

    账本里会话的身份是 `k`（原本是书的路径）。云端存的是 UID 形状，写回本机时必须换回路径，
    否则界面按路径分组就一条都对不上——**这一条真踩过**：一次同步之后 9 场会话的 k 全变成了
    16 位 UID，账本秒数一条没少，但"浙江村"名下一条会话都看不见了。

    本机没有这本书（只存在于别的机器上）时保留 UID：宁可显示得难看，也不能丢数据、不能瞎编路径。
    """
    if not isinstance(view, dict):
        return {}

    def local_key(uid):
        paths = [p for p in (paths_by_uid.get(uid) or []) if p]
        for p in paths:
            if os.path.isfile(p):
                return p
        return paths[0] if paths else uid

    sessions = []
    for s in view.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        row = dict(s)
        row["k"] = local_key(str(row.get("k") or ""))
        sessions.append(row)
    books = {}
    for key, book in (view.get("books") or {}).items():
        if not isinstance(book, dict):
            continue
        row = dict(book)
        row["k"] = local_key(str(key or ""))
        books[row["k"]] = row
    out = {k: v for k, v in view.items()
           if k not in ("v", "schema", "device", "updated_at", "rev")}
    out["sessions"] = sessions
    out["books"] = books
    out["days"] = dict(view.get("days") or {})
    out.setdefault("version", 1)                            # 本机文件有 version，同步形状没有
    return out


def settings_to_local(view, config):
    out = {}
    for key, val in (view.get("values") or {}).items():
        if key in SYNC_SETTING_KEYS:
            out[key] = val
    return out


# ---------------------------------------------------------------- HTTP 客户端

class SyncError(Exception):
    pass


class Unauthorized(SyncError):
    pass


class Conflict(SyncError):
    def __init__(self, rev=""):
        super().__init__("conflict")
        self.rev = rev


def _safe_filename(name):
    """远端给的文件名一律当不可信：只取最后一段、去掉非法字符、限长。"""
    name = os.path.basename(str(name or "").replace("\\", "/").split("/")[-1])
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    if not name:
        return ""
    return name[-160:]


def pull_books(client, dest_dir, local_shas=None, log=None):
    """把云端有、本机没有的书下到书架目录（内容寻址：同一个 sha 不会重复下）。

    只在用户明确点了「从云端补齐书」时调用——不在同步轮里偷偷往磁盘写文件。
    """
    out = {"remote": 0, "downloaded": 0, "skipped": 0, "failed": [], "names": []}
    local_shas = set(local_shas or ())
    blobs = client.blob_list()
    out["remote"] = len(blobs)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        out["failed"].append("书架目录建不出来：%s" % e)
        return out
    for row in blobs:
        sha = str(row.get("sha") or "").lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            continue
        if sha in local_shas:
            out["skipped"] += 1
            continue
        name = _safe_filename(row.get("name") or "") or (sha[:16] + ".bin")
        dest = os.path.join(dest_dir, name)
        stem, ext = os.path.splitext(dest)
        n = 1
        while os.path.exists(dest):
            dest = "%s (%d)%s" % (stem, n, ext)
            n += 1
        try:
            client.blob_get(sha, dest)
            size = os.path.getsize(dest)
            want = int(row.get("size") or size)
            if want and size != want:
                out["failed"].append("%s 大小对不上（%d ≠ %d）" % (name, size, want))
                try:
                    os.remove(dest)
                except OSError:
                    pass
                continue
            out["downloaded"] += 1
            out["names"].append(os.path.basename(dest))
            if log:
                log("补下一本书：%s（%.1f MB）" % (os.path.basename(dest), size / 1048576.0))
        except (SyncError, OSError) as e:
            out["failed"].append("%s: %s" % (name, e))
            try:
                if os.path.exists(dest + ".part"):
                    os.remove(dest + ".part")
            except OSError:
                pass
    return out


class SyncClient:
    """HTTP 客户端。用 `http.client` 而不是 urllib：要好 keep-alive（一次同步十几秒全耗在建连上），
    也要能直接看到状态码（412/404 不该靠解析错误字符串）。"""

    def __init__(self, base_url, token, timeout=HTTP_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        try:                                     # 传元组也认，取大的那个当总量
            self.timeout = float(timeout[1] if isinstance(timeout, (tuple, list)) else timeout)
        except (TypeError, ValueError, IndexError):
            self.timeout = float(HTTP_TIMEOUT)
        u = urllib.parse.urlsplit(self.base_url)
        self.scheme = (u.scheme or "https").lower()
        self.host = u.hostname or ""
        self.port = u.port or (443 if self.scheme == "https" else 80)
        self.prefix = (u.path or "").rstrip("/")
        self._conn = None
        self.calls = {"GET": 0, "PUT": 0, "POST": 0}      # 每轮清零：用来量"这一轮到底发了几个请求"

    # --- 连接 ---
    def close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _connection(self):
        if self._conn is not None:
            return self._conn
        if not self.host:
            raise SyncError("地址不对：%r" % self.base_url)
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        self._conn = conn
        return conn

    def _headers(self, extra=None):
        h = {"Authorization": "Bearer " + self.token, "User-Agent": "StealthReader-sync/1",
             "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def _request(self, method, path, body=None, headers=None, retry=True):
        """返回 (状态码, 小写键的响应头 dict, 响应体 bytes)。网络层失败抛 SyncError。"""
        h = self._headers(headers)
        self.calls[method] = self.calls.get(method, 0) + 1
        if body is not None:
            h["Content-Length"] = str(len(body))
        last = None
        for attempt in (0, 1):
            conn = self._connection()
            try:
                conn.request(method, self.prefix + path, body=body, headers=h)
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
            except (http.client.HTTPException, ssl.SSLError, socket.timeout, OSError) as e:
                self.close()
                last = e
                if attempt == 0 and retry:
                    continue
                break
        raise SyncError("连不上 %s：%s" % (self.host, last))

    def _json(self, method, path, body=None, headers=None, ok=(200,), retry=True):
        status, hdrs, raw = self._request(method, path, body, headers, retry=retry)
        if status == 401:
            raise Unauthorized("token 不对（401）")
        if status == 412:
            rev = ""
            try:
                rev = str((json.loads(raw.decode("utf-8")) or {}).get("rev") or "")
            except Exception:
                pass
            raise Conflict(rev)
        if status not in ok:
            msg = raw[:160].decode("utf-8", "replace")
            raise SyncError("HTTP %s %s" % (status, msg))
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise SyncError("回的不是 JSON：%r" % raw[:120])
        if not isinstance(data, dict):
            raise SyncError("回的结构不对")
        if not data.get("ok", False) and status < 400:
            raise SyncError(str(data.get("err") or "服务端说不行"))
        return data, hdrs

    # --- 端点 ---
    def ping(self):
        t0 = time.time()
        data, _ = self._json("GET", "/v1/ping")
        data["latency_ms"] = int((time.time() - t0) * 1000)
        return data

    def manifest(self):
        data, _ = self._json("GET", "/v1/manifest")
        return data.get("kinds") or {}

    def get_state(self, kind):
        status, hdrs, raw = self._request("GET", "/v1/state/" + kind)
        if status == 401:
            raise Unauthorized("token 不对（401）")
        if status == 404:
            return None, ""
        if status != 200:
            raise SyncError("HTTP %s 拉 %s 失败" % (status, kind))
        rev = (hdrs.get("etag") or "").strip().strip('"')
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise SyncError("%s 解析失败（远端对象坏了？）" % kind)
        if not isinstance(payload, dict):
            raise SyncError("%s 结构不对" % kind)
        return payload, rev

    def put_state(self, kind, payload, rev="", create=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        extra = {"Content-Type": "application/json; charset=utf-8"}
        if create:
            extra["X-Create"] = "1"
        elif rev:
            extra["If-Match"] = rev
        else:
            raise SyncError("缺 rev：服务端不接受无条件覆盖")
        data, _ = self._json("PUT", "/v1/state/" + kind, body, extra)
        return str(data.get("rev") or "").strip()

    def blob_check(self, shas):
        body = json.dumps({"sha256": list(shas)}).encode("utf-8")
        data, _ = self._json("POST", "/v1/blob/check", body, {"Content-Type": "application/json"})
        return list(data.get("missing") or [])

    def blob_put(self, sha, path, prog=None, name=None, fix_name=False):
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            body = f.read()
        extra = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
        if name:
            extra["X-File-Name"] = urllib.parse.quote(str(name))
        if fix_name:
            extra["X-Fix-Name"] = "1"
        data, _ = self._json("PUT", "/v1/blob/" + sha, body, extra, retry=False)
        if prog:
            prog(sha, size, True)
        return data

    def blob_list(self):
        """远端有哪些书：[{sha, size, name}]。"""
        data, _ = self._json("GET", "/v1/blobs")
        rows = data.get("blobs")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def put_lite_index(self, uid, payload):
        """章节偏移表：阅读页拿它把"第几个字"换成"跳到哪一章"。派生数据，随便覆盖。"""
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        status, _hdrs, raw = self._request(
            "PUT", "/v1/lite/%s/index.json" % uid, body,
            {"Content-Type": "application/json; charset=utf-8"})
        if status not in (200, 201):
            raise SyncError("HTTP %s 传章节表失败 %s" % (status, raw[:120]))
        return len(body)

    def blob_get(self, sha, dest):
        status, hdrs, raw = self._request("GET", "/v1/blob/" + sha)
        if status == 404:
            raise SyncError("远端没有这个文件")
        if status != 200:
            raise SyncError("HTTP %s 下书失败" % status)
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, dest)
        return dest


# ---------------------------------------------------------------- 后台线程

class SyncWorker(threading.Thread):
    """一次只做一轮；调用方（主线程）负责什么时候叫它。

    `request(snapshot)` 把"主线程此刻内存里的数据副本"放进队列（只留最新一份，多了合并掉），
    `apply_cb(payload)` 由本线程调用，payload 是**本地形状**的合并结果，主线程拿到后写回内存再存盘。
    """

    def __init__(self, *, base_url, token, device_name, state_path, apply_cb,
                 log=None, kinds=SYNC_KINDS, books=False, state=None, index_fn=None):
        super().__init__(daemon=True, name="StealthReaderSync")
        self.client = SyncClient(base_url, token)
        self.device_name = device_name or "unknown"
        self.state_path = state_path
        self.apply_cb = apply_cb
        self.log = log or (lambda msg: None)
        self.kinds = tuple(kinds or SYNC_KINDS)
        self.books = bool(books)
        # 章节偏移表的生成器（main.py 传进来：它已经有全文解析，别在引擎里再写一遍）
        self.index_fn = index_fn
        self.state = state if isinstance(state, dict) else read_json(state_path, {})
        self._q = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_result = {}
        self.backoff = 0

    # --- 对外 ---
    def request(self, snapshot, reason="change"):
        with self._lock:
            self._q = [(snapshot, reason)]           # 只留最新，避免排队积压
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def save_state(self):
        try:
            write_json_atomic(self.state_path, self.state, keep_bak=1)
        except Exception as e:
            self.log("同步状态写不进去：%s" % e)

    # --- 线程体 ---
    def run(self):
        while not self._stop.is_set():
            self._wake.wait(timeout=5.0)
            self._wake.clear()
            with self._lock:
                item = self._q.pop() if self._q else None
            if not item:
                continue
            snapshot, reason = item
            try:
                self._round(snapshot, reason)
                self.backoff = 0
            except Unauthorized as e:
                self.last_result = {"ok": False, "error": str(e), "fatal": True}
                self.log("同步停了：%s" % e)
                self.state["last_error"] = str(e)
                self.save_state()
            except Exception as e:
                self.last_result = {"ok": False, "error": str(e)}
                self.state["last_error"] = str(e)
                self.save_state()
                self.backoff = min(self.backoff * 2 or 5, 900)
                self.log("同步失败：%s（%d 秒后重试）" % (e, self.backoff))
                if not self._stop.is_set():
                    time.sleep(self.backoff)

    # --- 一轮 ---
    def _round(self, snapshot, reason):
        lock = FileLock(self.state_path + ".lock")
        if not lock.__enter__():
            self.log("另一个进程在同步，这轮跳过")
            return
        payload = None
        self.client.calls = {"GET": 0, "PUT": 0, "POST": 0}     # 这一轮发了几个请求
        try:
            payload = self._merge_all(snapshot, reason)
            if payload.get("kinds"):
                self.apply_cb(payload)                       # 交给主线程写回（这里是纯数据）
            self.backoff = 0
        finally:
            lock.__exit__(None, None, None)
        return payload

    def _merge_all(self, snapshot, reason):
        config = snapshot.get("config") or {}
        annotations = snapshot.get("annotations") or {}
        stats = snapshot.get("stats") or {}
        cache = self.state.setdefault("uids", {})
        uidmap = uid_map_for(config, cache)
        paths_by_uid = uid_to_paths(uidmap)
        self._collect_tombstones(annotations, uidmap, cache)
        settings_clock = self._settings_clock_for(config)

        out = {"reason": reason, "kinds": {}, "notes": [], "device": self.device_name}
        manifest = self.client.manifest()
        self.state["kinds"] = {k: dict(v) for k, v in manifest.items()}

        builders = {
            "progress": lambda: progress_view(config, uidmap, self.device_name, cache),
            "annotations": lambda: annotations_view(annotations, uidmap, self.device_name,
                                                    self.state.get("tombstones"), cache),
            "stats": lambda: stats_view(stats, uidmap, self.device_name),
            "settings": lambda: settings_view(config, settings_clock, self.device_name),
        }
        mergers = {
            "progress": merge_progress,
            "annotations": merge_annotations,
            "stats": merge_stats,
            "settings": merge_settings,
        }

        for kind in self.kinds:
            builder = builders.get(kind)
            if builder is None:
                continue
            try:
                self._sync_kind(kind, builder(), mergers[kind], manifest,
                                uidmap, paths_by_uid, out, annotations, config)
            except Unauthorized:
                raise
            except SyncError as e:
                # 一类没成不拖累其他三类：离线时最容易在第一个 kind 上就炸，后面全白等
                self.log("%s 这轮没成：%s" % (kind, e))
                out["notes"].append("%s 失败：%s" % (kind, e))

        if self.books:
            out["books"] = self._sync_books(snapshot)

        out["calls"] = dict(self.client.calls)
        out["skipped"] = int(self.state.get("skipped") or 0)
        self.state["last_ok_at"] = _now()
        self.state["last_error"] = ""
        self.save_state()
        self.last_result = {"ok": True, "kinds": list(out["kinds"]), "notes": out["notes"],
                            "at": self.state["last_ok_at"], "calls": dict(self.client.calls),
                            "skipped": int(self.state.get("skipped") or 0)}
        return out

    def _sync_kind(self, kind, local_view, merger, manifest, uidmap, paths_by_uid,
                   out, annotations, config):
        """一个 kind 跑一轮，尽量少发请求：

        - 远端 rev 跟上一轮记下的一样 → 远端内容就是缓存里那份，**不发 GET**
        - 合并结果跟缓存里那份内容相同 → **不发 PUT**（不换 rev，不给别的机器制造无用功）

        缓存在 `state["remote"][kind]` 里：rev + 上次的内容 + 内容指纹。四个 kind 一共十几 KB，
        塞进 sync_state.json 完全无感。
        """
        cached = (self.state.get("remote") or {}).get(kind) or {}
        mrev = str(((manifest.get(kind) or {}).get("rev")) or "")
        remote_view, rev = None, ""
        if mrev and cached.get("rev") == mrev and isinstance(cached.get("view"), dict):
            remote_view, rev = cached["view"], mrev              # 远端没动：白拿一份
        elif kind in manifest:
            remote_view, rev = self.client.get_state(kind)
            if remote_view is not None and rev:
                self.state.setdefault("kinds", {}).setdefault(kind, {})["rev"] = rev

        if remote_view is None:
            merged = local_view
            merged["updated_at"] = _now()
            new_rev = self.client.put_state(kind, merged, create=True)
        else:
            for attempt in range(MAX_CONFLICT_RETRY):
                merged = merger(local_view, remote_view)
                notes = []
                if isinstance(merged, tuple):
                    merged, notes = merged
                    out["notes"].extend(notes)
                merged["updated_at"] = _now()
                # 跟**远端那份**比内容（不是跟上一轮的缓存比）：远端刚被别的机器改过、
                # 我们只是拉下来，合并结果就等于远端，这时写回去纯属白写一次、白换一个 rev。
                if _view_hash(merged) == _view_hash(remote_view):
                    new_rev = rev                               # 内容没变：不写
                    self.state["skipped"] = int(self.state.get("skipped") or 0) + 1
                    if SYNC_DEBUG:
                        self.log("  %s：跳过（内容与远端一致）" % kind)
                    break
                if SYNC_DEBUG:
                    self.log("  %s：要写，差在 %s" % (kind, _diff_brief(remote_view, merged)))
                try:
                    new_rev = self.client.put_state(kind, merged, rev=rev)
                    break
                except Conflict:
                    self.log("%s 冲突（第 %d 次），重新拉一遍再并" % (kind, attempt + 1))
                    remote_view, rev = self.client.get_state(kind)
                    if remote_view is None:
                        new_rev = self.client.put_state(kind, merged, create=True)
                        break
            else:
                self.log("%s 连着 %d 次冲突，这轮先放下" % (kind, MAX_CONFLICT_RETRY))
                return

        self.state.setdefault("kinds", {})[kind] = {"rev": new_rev, "at": _now()}
        self.state.setdefault("remote", {})[kind] = {
            "rev": new_rev, "view": merged, "hash": _view_hash(merged), "at": _now()}

        # 本地形状（主线程拿这个写回）
        if kind == "progress":
            out["kinds"]["progress"] = progress_to_local(merged, uidmap, paths_by_uid, config)
            out["kinds"]["progress"]["_schema_ok"] = int(_num(merged.get("schema"))) == EXTRACT_SCHEMA
        elif kind == "annotations":
            out["kinds"]["annotations"] = annotations_to_local(
                merged, uidmap, paths_by_uid, annotations,
                schema_ok=int(_num(merged.get("schema"))) == EXTRACT_SCHEMA)
            self.state["tombstones"] = {uid: {t["id"]: t["at"] for t in (row.get("tombstones") or [])}
                                        for uid, row in (merged.get("books") or {}).items()}
            # 记住"合并之后"有哪些条目：下一轮拿本地跟它比，少掉的才算本地删的
            self.state["annotation_ids"] = {
                uid: {kind2: sorted(str(it.get("id")) for it in (row.get(kind2) or []) if it.get("id"))
                      for kind2 in ("highlights", "bookmarks")}
                for uid, row in (merged.get("books") or {}).items()}
        elif kind == "stats":
            out["kinds"]["stats"] = stats_to_local(merged, uidmap, paths_by_uid)
        elif kind == "settings":
            out["kinds"]["settings"] = settings_to_local(merged, config)
            self.state["settings_clock"] = dict(merged.get("clock") or {}) if isinstance(merged, dict) else {}

    # --- 两件"记账"的活：墓碑、设置时钟 ---
    def _collect_tombstones(self, annotations, uidmap, cache=None):
        """本地少了哪些划线/书签 → 记成墓碑，下一轮合并就会把它们从远端也删掉。

        只在"这个 UID 本机还在"时记：整条记录都不见了（文件删了、记录没读进来）不算删除，
        否则一次读盘不全就会把云端笔记清空。
        """
        tombs = self.state.setdefault("tombstones", {})
        prev = self.state.get("annotation_ids") or {}
        now = _now()
        cur = {}
        for path, store in (annotations or {}).items():
            uid = uidmap.get(str(path))
            if not uid and cache is not None:
                uid, _info = uid_for(str(path), cache)
            if not uid or not isinstance(store, dict):
                continue
            row = cur.setdefault(uid, {})
            for kind in ("highlights", "bookmarks"):
                ids = {str(i.get("id")) for i in (store.get(kind) or [])
                       if isinstance(i, dict) and i.get("id")}
                row[kind] = sorted(ids)
                old = set((prev.get(uid) or {}).get(kind) or [])
                for gone in old - ids:
                    tombs.setdefault(uid, {})[gone] = now
                    self.log("本地删掉了 %s，记墓碑" % gone)
        for uid, row in (prev or {}).items():        # 本机整个消失的 UID：原样留着，别动
            cur.setdefault(uid, row)
        self.state["annotation_ids"] = cur

    def _settings_clock_for(self, config):
        """逐键时钟：值真的变了才记"现在"；第一次见到给哨兵 1.0（等同于"很久以前改的"）。

         只用哨兵不用"现在"：否则两台机器第一次同步时，谁先跑谁就"更新"，把对方真实设置盖掉。
        """
        clock = self.state.setdefault("settings_clock", {})
        prev = self.state.get("settings_values") or {}
        now = _now()
        for key in SYNC_SETTING_KEYS:
            if key not in config:
                continue
            if key not in prev:
                clock.setdefault(key, 1.0)
            elif prev.get(key) != config[key]:
                clock[key] = now
        self.state["settings_values"] = {k: config[k] for k in SYNC_SETTING_KEYS if k in config}
        return clock

    def _sync_book_indexes(self, sha_by_path):
        """给阅读页（手机/Kindle/网页）用的章节偏移表：一本书一个几 KB 的 JSON。

        只有"书没动过"才跳过；`book_index` 里记 (uid, size, mtime)，改一个字就重算重传。
        算不出来（坏书/纯图片）就跳过，不因为一本书失败拖垮整轮。
        """
        if not self.index_fn:
            return 0
        cache = self.state.setdefault("book_index", {})
        done = 0
        for path, sha in sorted(sha_by_path.items()):
            key = os.path.normcase(os.path.abspath(path))
            try:
                st = os.stat(path)
            except OSError:
                continue
            row = cache.get(key) or {}
            if (row.get("uid") == sha[:16] and row.get("size") == st.st_size
                    and row.get("mtime") == int(st.st_mtime)):
                continue
            try:
                payload = self.index_fn(path)
            except Exception as e:
                self.log("章节表算不出来：%s（%s）" % (os.path.basename(path), e))
                continue
            if not isinstance(payload, dict) or not _num(payload.get("total")):
                continue
            payload = dict(payload)
            payload["uid"] = sha[:16]
            payload["schema"] = int(_num(payload.get("schema")) or 1)
            payload["generated_at"] = _now()
            try:
                self.client.put_lite_index(sha[:16], payload)
            except SyncError as e:
                self.log("章节表传不上去：%s（%s）" % (os.path.basename(path), e))
                continue
            cache[key] = {"uid": sha[:16], "size": st.st_size, "mtime": int(st.st_mtime)}
            done += 1
        if done:
            self.log("章节表：上传 %d 份（阅读页按它定位）" % done)
        return done

    def _sync_books(self, snapshot):
        """书文件：内容寻址，只传"本机有、远端没记过"的，顺带给老对象补原名。永不删除远端。

        两个缓存让它在"书没变"时**一个请求都不发**：
        - `book_hash`：路径 -> (大小, mtime, sha)。19 MB 的书不能每轮重算一遍 sha。
        - `book_uploaded`：sha -> 远端记着的原名。有这个就不用再问 `/v1/blobs`。
        改名也算变化（原名要跟着更新），但只在真改名时才重传那一本。
        """
        result = {"checked": 0, "uploaded": 0, "renamed": 0, "hashed": 0, "failed": []}
        paths = [p for p in (snapshot.get("book_paths") or []) if os.path.isfile(p)]
        if not paths:
            return result
        cache = self.state.setdefault("book_hash", {})
        uploaded = self.state.setdefault("book_uploaded", {})
        sha_by_path = {}
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            try:
                st = os.stat(path)
            except OSError:
                continue
            row = cache.get(key) or {}
            if (row.get("sha") and row.get("size") == st.st_size
                    and row.get("mtime") == int(st.st_mtime)):
                sha_by_path[path] = row["sha"]             # 没动过：不重算
                continue
            try:
                sha = _hash_file(path)
            except OSError:
                continue
            cache[key] = {"size": st.st_size, "mtime": int(st.st_mtime), "sha": sha}
            result["hashed"] += 1
            sha_by_path[path] = sha
        result["checked"] = len(sha_by_path)

        result["indexed"] = self._sync_book_indexes(sha_by_path)

        todo = []
        for path, sha in sha_by_path.items():
            name = os.path.basename(path)
            if uploaded.get(sha) == name:
                continue                                   # 传过、名字也一样
            todo.append((path, sha, name))
        if not todo:
            return result                                  # 一个字节都没变：不问远端

        try:
            known = {str(r.get("sha") or ""): r for r in self.client.blob_list()}
        except SyncError as e:
            result["failed"].append(str(e))
            return result
        for path, sha, name in todo:
            row = known.get(sha)
            if row is not None and str(row.get("name") or "") == name:
                uploaded[sha] = name                       # 远端已经有，且名字对得上
                continue
            try:
                self.client.blob_put(sha, path, name=name, fix_name=row is not None)
                uploaded[sha] = name
                if row is None:
                    result["uploaded"] += 1
                else:
                    result["renamed"] += 1
            except SyncError as e:
                result["failed"].append("%s: %s" % (name, e))
        self.log("书文件：查 %d 本（重算 %d 本），新传 %d 本，补名 %d 本" % (
            result["checked"], result["hashed"], result["uploaded"], result["renamed"]))
        return result


# ---------------------------------------------------------------- 自检（部署完先跑这个）

def selftest(base_url, token, verbose=True):
    out = []
    client = SyncClient(base_url, token)

    def step(name, fn):
        t0 = time.time()
        try:
            info = fn()
            out.append((name, True, "%d ms %s" % ((time.time() - t0) * 1000, info or "")))
        except Exception as e:
            out.append((name, False, "%s" % e))
            raise

    try:
        step("ping", lambda: "server_time=%.0f" % _num(client.ping().get("server_time")))
        kinds = step("manifest", lambda: "对象 %d 个" % len(client.manifest())) or []

        # 状态读写：先读旧的，测完原样放回去。
        # 以前这里是凭空写一条 "selftest" 书进去，于是真对象的 progress 里永远多一本假书（踩过一次）。
        probe_kind = "progress"
        try:
            orig, orig_rev = client.get_state(probe_kind)
        except SyncError:
            orig, orig_rev = None, ""
        base = dict(orig) if isinstance(orig, dict) else {
            "v": STATE_VERSION, "schema": EXTRACT_SCHEMA, "device": "selftest", "books": {}}
        base.pop("_selftest", None)
        holder = {"rev": "", "wrote": False}

        def write_first():
            payload = dict(base)
            payload["_selftest"] = _now()
            if orig_rev:
                holder["rev"] = client.put_state(probe_kind, payload, rev=orig_rev)
            else:
                holder["rev"] = client.put_state(probe_kind, payload, create=True)
            holder["wrote"] = True
            return "写入 rev=%s（测完放回）" % holder["rev"][:8]

        step("PUT", write_first)
        step("GET", lambda: "读到 %d 字节" % len(json.dumps(client.get_state(probe_kind)[0])))

        def stale_write():
            _, rev = client.get_state(probe_kind)
            try:
                client.put_state(probe_kind, base, rev=rev + "x")
                return "居然没拒绝"
            except Conflict:
                return "错 rev 被 412 拒绝了（对）"

        step("PUT（错 rev 必须 412）", stale_write)

        def restore():
            if not holder["wrote"]:
                return "没写进去，不用放回"
            client.put_state(probe_kind, orig if orig is not None else
                             {"v": STATE_VERSION, "schema": EXTRACT_SCHEMA,
                              "device": "selftest", "books": {}}, rev=holder["rev"])
            return "原对象已放回"

        step("还原", restore)

        def blob():
            data = b"stealthreader selftest"
            sha = hashlib.sha256(data).hexdigest()
            missing = client.blob_check([sha])
            if sha in missing:
                tmp = os.path.join(tempfile.gettempdir(), "sr-selftest.bin")
                with open(tmp, "wb") as f:
                    f.write(data)
                client.blob_put(sha, tmp)
                os.remove(tmp)
            back = os.path.join(tempfile.gettempdir(), "sr-selftest-back.bin")
            client.blob_get(sha, back)
            ok = open(back, "rb").read() == data
            os.remove(back)
            return "blob 往返一致" if ok else "blob 内容不一致！"

        step("blob 往返", blob)
    except Exception:
        pass
    if verbose:
        for name, ok, info in out:
            print(("  ok   " if ok else "  FAIL ") + name.ljust(22) + info)
        bad = [r for r in out if not r[1]]
        print("\n%d / %d 步通过" % (len(out) - len(bad), len(out)))
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("用法：python sync_engine.py --selftest https://sync.example.com <token>")
        return 0
    if argv[0] == "--selftest":
        if len(argv) < 3:
            print("要 base_url 和 token")
            return 2
        res = selftest(argv[1], argv[2])
        return 0 if all(ok for _, ok, _ in res) else 1
    print("不认识的参数：%s" % argv[0])
    return 2


if __name__ == "__main__":
    sys.exit(main())
