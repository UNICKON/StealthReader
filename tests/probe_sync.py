#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步功能的自检：纯函数 + 两台"假机器"跑真链路（假后端在内存里）。

    python tests/probe_sync.py           # 全部
    python tests/probe_sync.py --pure    # 只跑纯函数（不启后端）

不碰真数据：每台"机器"都是 %TEMP% 下的一个临时目录。
"""
import copy
import hashlib
import os
import shutil
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
sys.path.insert(0, SR)
sys.path.insert(0, HERE)

import sync_engine as se
from fake_sync_server import FakeSyncServer

PASS = 0
FAIL = 0
BAD = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        BAD.append(name + (("   << " + str(extra)) if extra else ""))
        print("  FAIL " + name + (("   << " + str(extra)) if extra else ""))


def section(title):
    print("\n=== %s ===" % title)


# ------------------------------------------------------------------ 假机器

class Device:
    """一台"电脑"：自己的 config / annotations / stats / sync_state。"""

    def __init__(self, name, root, book_path=None):
        self.name = name
        self.root = os.path.join(root, name)
        os.makedirs(self.root, exist_ok=True)
        self.book_path = book_path
        self.config = {"local_book_positions": {}, "local_book_totals": {},
                       "local_book_times": {}, "local_book_hidden": [], "font_size": 17}
        if book_path:
            self.config["local_book_positions"][book_path] = 0
            self.config["local_book_totals"][book_path] = 1000
            self.config["local_book_times"][book_path] = 1762950000.0
        self.annotations = {}
        self.stats = {"days": {}, "books": {}, "sessions": []}
        self.state_path = os.path.join(self.root, "sync_state.json")
        self.applied = []
        self.worker = None

    def make_worker(self, srv, books=False):
        self.worker = se.SyncWorker(
            base_url=srv.base_url, token=srv.token, device_name=self.name,
            state_path=self.state_path, apply_cb=self.apply, log=lambda m: None,
            books=books)
        return self.worker

    def snapshot(self):
        return {"config": copy.deepcopy(self.config),
                "annotations": copy.deepcopy(self.annotations),
                "stats": copy.deepcopy(self.stats),
                "book_paths": [self.book_path] if self.book_path and os.path.isfile(self.book_path) else []}

    def apply(self, payload):
        self.applied.append(payload)
        kinds = payload.get("kinds") or {}
        if "progress" in kinds:
            for key in ("local_book_positions", "local_book_totals", "local_book_times", "local_book_hidden"):
                if key in kinds["progress"]:
                    self.config[key] = kinds["progress"][key]
        if "annotations" in kinds:
            self.annotations = kinds["annotations"]
        if "stats" in kinds:
            self.stats = kinds["stats"]
        if "settings" in kinds:
            self.config.update(kinds["settings"])

    def round(self, reason="test"):
        return self.worker._round(self.snapshot(), reason)

    # --- 读书的动作（模拟真人）---
    def read(self, sec, chars, start, title="雨"):
        se.rs.add_span(self.stats, se.os.path.normcase(os.path.abspath(self.book_path)), title,
                       start, sec, chars, end_ts=start + sec)

    def highlight(self, text, pos=10, hid=None):
        key = self.book_path or "u1"
        store = self.annotations.setdefault(key, {"highlights": [], "bookmarks": []})
        store["highlights"].append({"id": hid or ("h%d" % int(time.time() * 1000)),
                                    "start": pos, "end": pos + len(text), "text": text,
                                    "color": "#ffd54f", "created": "2026-11-13 10:00"})

    def drop_highlight(self, hid):
        key = self.book_path or "u1"
        store = self.annotations.get(key) or {}
        store["highlights"] = [h for h in store.get("highlights") or [] if h.get("id") != hid]


# ------------------------------------------------------------------ 纯函数

def run_pure():
    T = 1762950000.0
    section("进度合并")
    lp = {"v": 1, "schema": 1, "updated_at": T, "device": "A",
          "books": {"u1": {"title": "雨", "pos": 100, "total": 1000, "at": T - 100, "dev": "A"}}}
    rp = {"v": 1, "schema": 1, "updated_at": T, "device": "B",
          "books": {"u1": {"title": "雨", "pos": 300, "total": 1000, "at": T, "dev": "B"},
                    "u9": {"title": "远端的书", "pos": 7, "total": 800, "at": T}}}
    m = se.merge_progress(lp, rp)
    check("同 UID 取 at 更晚的（300）", m["books"]["u1"]["pos"] == 300, m["books"]["u1"]["pos"])
    check("远端独有的书并进来", m["books"]["u9"]["pos"] == 7)
    check("合并可交换", json_eq(se.merge_progress(rp, lp)["books"], m["books"]))
    check("合并幂等", json_eq(se.merge_progress(m, m)["books"], m["books"]))
    check("行形状一致（七个字段）", set(m["books"]["u1"]) == {"title", "pos", "total", "at", "dev", "hidden", "stale"},
          sorted(m["books"]["u1"]))

    section("划线合并")
    la = {"v": 1, "schema": 1, "updated_at": T, "device": "A",
          "books": {"u1": {"highlights": [{"id": "h1", "start": 1, "end": 3, "text": "甲", "color": "#f00", "at": T - 10}],
                           "bookmarks": [], "tombstones": []}}}
    ra = {"v": 1, "schema": 1, "updated_at": T, "device": "B",
          "books": {"u1": {"highlights": [{"id": "h1", "start": 1, "end": 3, "text": "甲", "note": "B 的笔记", "at": T}],
                           "bookmarks": [{"id": "b1", "pos": 9, "snippet": "页", "at": T}], "tombstones": []}}}
    ma = se.merge_annotations(la, ra)
    h1 = [h for h in ma["books"]["u1"]["highlights"] if h["id"] == "h1"][0]
    check("同 id 取 at 更晚（B 的笔记带上了）", h1.get("note") == "B 的笔记", h1)
    check("本地独有的 color 没被冲掉", h1.get("color") == "#f00", h1)
    check("书签并进来", [b["id"] for b in ma["books"]["u1"]["bookmarks"]] == ["b1"])
    tomb = {"v": 1, "schema": 1, "updated_at": T, "device": "B",
            "books": {"u1": {"highlights": [], "bookmarks": [], "tombstones": [{"id": "h1", "at": T + 1}]}}}
    check("墓碑更新就真删", se.merge_annotations(la, tomb)["books"]["u1"]["highlights"] == [])
    old = {"v": 1, "schema": 1, "updated_at": T, "device": "B",
           "books": {"u1": {"highlights": [], "bookmarks": [], "tombstones": [{"id": "h1", "at": T - 99}]}}}
    check("墓碑比条目旧 → 不删", len(se.merge_annotations(la, old)["books"]["u1"]["highlights"]) == 1)

    section("账本合并（不能越并越大）")
    A = {"sessions": [{"k": "u1", "title": "雨", "start": 1000.0, "sec": 1800.0, "chars": 9000.0, "end": 2800.0}],
         "days": {"2026-11-12": {"n": 1, "sec": 1800.0, "chars": 9000.0}},
         "books": {"u1": {"n": 1, "sec": 1800.0, "chars": 9000.0, "first": 1000.0, "last": 2800.0}}}
    B = {"sessions": [{"k": "u1", "title": "雨", "start": 1000.0, "sec": 2160.0, "chars": 10800.0, "end": 3160.0}],
         "days": {"2026-11-12": {"n": 1, "sec": 2160.0, "chars": 10800.0}},
         "books": {"u1": {"n": 1, "sec": 2160.0, "chars": 10800.0, "first": 1000.0, "last": 3160.0}}}
    st, _ = se.merge_stats(A, B)
    check("同一场只留一条", len(st["sessions"]) == 1, st["sessions"])
    check("同一场取 end 更晚（2160，不是 3960）", abs(st["sessions"][0]["sec"] - 2160.0) < 0.6, st["sessions"][0]["sec"])
    check("幂等", json_eq(se.merge_stats(st, B)[0]["sessions"], st["sessions"]))
    check("可交换", json_eq(se.merge_stats(B, A)[0]["sessions"], st["sessions"]))
    cur = st
    for _ in range(3):
        cur, _ = se.merge_stats(cur, B)
    check("三轮之后还是 2160", abs(cur["sessions"][0]["sec"] - 2160.0) < 0.6, cur["sessions"][0]["sec"])
    check("日账不翻倍", abs(cur["days"]["2026-11-12"]["sec"] - 2160.0) < 60, cur["days"])
    check("书账 last 取更晚", abs(float(cur["books"]["u1"]["last"]) - 3160.0) < 1, cur["books"]["u1"])

    # 不同起点（真新增）才应该相加
    C = {"sessions": [{"k": "u1", "title": "雨", "start": 9000.0, "sec": 600.0, "chars": 3000.0, "end": 9600.0}],
         "days": {}, "books": {}}
    st2, _ = se.merge_stats(st, C)
    check("另一天才相加（2160+600）", abs(sum(s["sec"] for s in st2["sessions"]) - 2760.0) < 1,
          [s["sec"] for s in st2["sessions"]])

    # 超 400 场
    many = {"sessions": [{"k": "b%d" % (i % 5), "title": "书%d" % (i % 5), "start": 1e5 + i * 2000,
                          "sec": 600.0, "chars": 3000.0, "end": 1e5 + i * 2000 + 600} for i in range(500)],
            "days": {"2024-01-01": {"n": 2, "sec": 5000.0, "chars": 20000.0}},
            "books": {"b1": {"n": 9, "sec": 9999.0, "chars": 50000.0, "first": 1.0, "last": 2.0}}}
    mst, _ = se.merge_stats(many, {"sessions": [], "days": {}, "books": {}})
    check("超 400 场的老日账还在", abs(mst["days"]["2024-01-01"]["sec"] - 5000.0) < 1, mst["days"].get("2024-01-01"))
    check("书账 first 不丢", float(mst["books"]["b1"]["first"]) == 1.0, mst["books"]["b1"])

    section("设置（逐键时钟）")
    ls = {"values": {"font_size": 17, "text_color": "a"}, "clock": {"font_size": T - 500, "text_color": T}}
    rsx = {"values": {"font_size": 18, "text_color": "b"}, "clock": {"font_size": T - 100, "text_color": T - 900}}
    view, _ = se.merge_settings(ls, rsx)
    vals = view["values"]
    check("字号用时钟更新的 B（18）", vals["font_size"] == 18, vals)
    check("颜色用时钟更新的 A（a）", vals["text_color"] == "a", vals)
    check("设置视图带时钟（能再传回远端）", view.get("clock", {}).get("font_size") == T - 100, view.get("clock"))

    section("书身份")
    tmp = tempfile.mkdtemp(prefix="sr_uid_")
    try:
        p = os.path.join(tmp, "书.epub")
        with open(p, "wb") as f:
            f.write(b"content-x")
        cache = {}
        uid, info = se.uid_for(p, cache)
        check("内容 UID 16 位", len(uid) == 16 and not info.get("stale"), (uid, info))
        check("缓存命中", se.uid_for(p, cache)[0] == uid)
        gone = os.path.join(tmp, "没有.epub")
        g, gi = se.uid_for(gone, cache)
        check("缺文件 → 合成 UID + stale", g.startswith("x") and gi.get("stale"), (g, gi))
        check("合成 UID 稳定", se.synthetic_uid(gone) == g)
        same = os.path.join(tmp, "同一个内容改了名字.epub")
        with open(same, "wb") as f:
            f.write(b"content-x")
        check("同内容不同路径 → 同一个 UID", se.uid_for(same, cache)[0] == uid)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    section("账本写回本机键")
    tmp2 = tempfile.mkdtemp(prefix="sr_stl_")
    try:
        p2 = os.path.join(tmp2, "跨越边界的社区.epub")
        with open(p2, "wb") as f:
            f.write(b"z")
        cache2 = {}
        uid2, _ = se.uid_for(p2, cache2)
        cfg2 = {"local_book_positions": {p2: 10}}
        uidmap2 = se.uid_map_for(cfg2, cache2)
        paths2 = se.uid_to_paths(uidmap2)
        view2 = {"v": 1, "device": "A", "updated_at": 1.0,
                 "sessions": [{"k": uid2, "sec": 100, "start": 1.0}],
                 "days": {"2026-11-10": {"n": 1, "sec": 100, "chars": 5}},
                 "books": {uid2: {"k": uid2, "title": "跨越边界的社区", "sec": 100}}}
        loc = se.stats_to_local(view2, uidmap2, paths2)
        check("会话 k 换回本机路径", (loc["sessions"][0]["k"] == p2), loc["sessions"][0]["k"])
        check("书账 k 也换回本机路径", list(loc["books"].keys()) == [p2], list(loc["books"].keys()))
        check("本机文件字段没丢（version / days）",
              loc.get("version") == 1 and "2026-11-10" in (loc.get("days") or {}), list(loc.keys()))
        check("同步专用字段没混进本机文件",
              not ({"v", "device", "updated_at"} & set(loc.keys())), list(loc.keys()))
        ghost = {"v": 1, "sessions": [{"k": "deadbeefdeadbeef", "sec": 5}], "books": {}, "days": {}}
        loc2 = se.stats_to_local(ghost, uidmap2, paths2)
        check("别的机器上的书保留 UID（不丢、不瞎编路径）",
              loc2["sessions"][0]["k"] == "deadbeefdeadbeef", loc2["sessions"][0]["k"])
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


def json_eq(a, b):
    import json
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(b, ensure_ascii=False, sort_keys=True)


# ------------------------------------------------------------------ 端到端

def run_e2e():
    root = tempfile.mkdtemp(prefix="sr_sync_")
    srv = FakeSyncServer(token="tok").start()
    book = os.path.join(root, "雨.epub")
    with open(book, "wb") as f:
        f.write(b"epub" * 1000)          # 4000 字节，够算 sha
    try:
        A = Device("A", root, book)
        B = Device("B", root, book)
        A.make_worker(srv)
        B.make_worker(srv)

        section("首传 + 拉取")
        A.read(1800, 9000, 1762950000.0)
        A.config["local_book_positions"][book] = 9000       # 读到第 9000 个字符
        A.highlight("第一段", 100, "h1")
        A.config["font_size"] = 18
        A.round("first")
        kinds = srv.state("progress") is not None and srv.state("annotations") is not None \
            and srv.state("stats") is not None and srv.state("settings") is not None
        check("四个对象都传到远端了", kinds)
        check("进度里有这本书（按 UID）", len((srv.state("progress") or {}).get("books") or {}) == 1,
              (srv.state("progress") or {}).get("books"))
        check("A 的 sync_state 落了盘", os.path.isfile(A.state_path))
        state = se.read_json(A.state_path)
        check("state 里记了 rev", bool((state.get("kinds") or {}).get("progress", {}).get("rev")), state.get("kinds"))
        check("state 里缓存了 UID", bool(state.get("uids")), list((state.get("uids") or {}).keys())[:1])

        B.round("pull")
        check("B 拿到了进度（本机表被填上）", B.config["local_book_positions"].get(book) == 9000,
              B.config["local_book_positions"].get(book))
        check("B 拿到了划线", [h["id"] for h in (B.annotations.get(book) or {}).get("highlights") or []] == ["h1"],
              B.annotations.get(book))
        check("B 拿到了字号 18", B.config.get("font_size") == 18, B.config.get("font_size"))
        check("B 的账本也到了（1800 秒）", abs(sum(s["sec"] for s in B.stats["sessions"]) - 1800) < 1,
              [s["sec"] for s in B.stats["sessions"]])
        got_stats = [p["kinds"]["stats"] for p in A.applied if (p.get("kinds") or {}).get("stats")]
        keys = [s.get("k") for s in (got_stats[-1]["sessions"] if got_stats else [])]
        check("写回本机的账本 k 是本机路径（不是 16 位 UID）",
              bool(keys) and all(os.path.normcase(k) == os.path.normcase(book) for k in keys), keys)

        section("两台各读一段（不同起点）→ 相加")
        B.read(600, 3000, 1762955000.0)
        B.highlight("B 划的", 500, "h2")
        B.round("B read")
        A.round("A pull")
        check("A 的账本变成 2400 秒", abs(sum(s["sec"] for s in A.stats["sessions"]) - 2400) < 1,
              [s["sec"] for s in A.stats["sessions"]])
        check("A 也有 B 的划线", sorted(h["id"] for h in (A.annotations.get(book) or {}).get("highlights") or []) == ["h1", "h2"],
              A.annotations.get(book))

        section("同一场被两边各改一次（真人会踩）")
        # A 继续读同一场（add_span 会并回同一场，sec 变大），B 那边还是旧快照
        A.read(300, 1500, 1762950000.0)
        before = sum(s["sec"] for s in A.stats["sessions"])
        B.round("B stale")            # B 用它的旧账本去并
        A.round("A merge")
        after = sum(s["sec"] for s in A.stats["sessions"])
        check("并完 A 的秒数没被吹大（最多涨 A 自己读的那 300）", after <= before + 305, (before, after))
        for _ in range(3):
            B.round("loop")
            A.round("loop")
        settled = sum(s["sec"] for s in A.stats["sessions"])
        check("再来三轮也不涨（收敛）", abs(settled - after) < 1, (after, settled))

        section("删除靠墓碑传播")
        A.drop_highlight("h2")
        A.round("A del")
        B.round("B pull del")
        check("B 那边 h2 也没了", sorted(h["id"] for h in (B.annotations.get(book) or {}).get("highlights") or []) == ["h1"],
              B.annotations.get(book))
        B.round("B again")
        A.round("A again")
        check("墓碑不会把删掉的拉回来", "h2" not in [h["id"] for h in (A.annotations.get(book) or {}).get("highlights") or []],
              A.annotations.get(book))

        section("设置双向")
        B.config["text_color"] = "rgba(9,9,9,255)"
        B.round("B set")
        A.round("A pull set")
        check("A 收到 B 改的颜色", A.config.get("text_color") == "rgba(9,9,9,255)", A.config.get("text_color"))
        check("字号还是 18（没被 B 的旧值盖掉）", A.config.get("font_size") == 18, A.config.get("font_size"))

        section("冲突：另一端刚写过")
        A.config["local_book_positions"][book] = 9500
        A.config["local_book_times"][book] = 1762960000.0
        srv.put_state_directly("progress", se.progress_view(B.config, se.uid_map_for(B.config, {}), "B"))
        A.round("conflict")
        check("遇到 412 自己拉回来重并了（不报错）", A.worker.last_result.get("ok") is True, A.worker.last_result)

        section("断线 / 401")
        srv.offline = True
        err = None
        try:
            A.round("offline")
        except Exception as e:
            err = e
        check("断线时抛的是 SyncError（不是崩）", isinstance(err, se.SyncError), repr(err))
        srv.offline = False
        A.round("back")
        check("恢复后能继续", A.worker.last_result.get("ok") is True, A.worker.last_result)
        srv.force_401 = True
        err2 = None
        try:
            A.round("401")
        except Exception as e:
            err2 = e
        check("401 抛 Unauthorized", isinstance(err2, se.Unauthorized), repr(err2))
        srv.force_401 = False

        section("书文件（内容寻址）")
        A.worker.books = True
        B.worker.books = True
        other = os.path.join(root, "同一个内容的副本.epub")
        with open(other, "wb") as f:
            f.write(b"epub" * 1000)
        B.config["local_book_positions"][other] = 0
        B.config["local_book_times"][other] = 1762950000.0
        B.make_worker(srv, books=True)
        A.make_worker(srv, books=True)
        A.round("upload book")
        check("A 把书传上去了", len(srv.blob_keys()) == 1, srv.blob_keys())
        B.round("check book")
        check("B 内容相同（不同路径）→ 不再重复上传", len(srv.blob_keys()) == 1, srv.blob_keys())
        check("B 的两个路径指向同一个 UID",
              len({se.uid_for(p, {})[0] for p in (book, other)}) == 1)

        section("书名 + 从云端补齐")
        client = se.SyncClient(srv.base_url, srv.token)
        rows = client.blob_list()
        check("书名跟着传上去了（另一台机器能还原原名）",
              any(r.get("name") == os.path.basename(book) for r in rows),
              [r.get("name") for r in rows])
        shelf_b = os.path.join(root, "machine-b-shelf")
        os.makedirs(shelf_b, exist_ok=True)
        res = se.pull_books(client, shelf_b, local_shas=set(), log=None)
        check("补齐：下了一本", res["downloaded"] == 1, res)
        got = os.path.join(shelf_b, os.path.basename(book))
        check("补下来的书是原名", os.path.isfile(got), os.listdir(shelf_b))
        with open(got, "rb") as f1, open(book, "rb") as f2:
            check("补下来的字节一致", f1.read() == f2.read())
        res2 = se.pull_books(client, shelf_b, local_shas={se._hash_file(got)}, log=None)
        check("本机已有的书不重复下", res2["downloaded"] == 0 and res2["skipped"] >= 1, res2)

        evil = os.path.join(root, "evil.epub")
        with open(evil, "wb") as f:
            f.write(b"evil-bytes" * 10)
        client.blob_put(se._hash_file(evil), evil, name="../../坑人.epub")
        res3 = se.pull_books(client, shelf_b, local_shas={se._hash_file(got)}, log=None)
        names = os.listdir(shelf_b)
        check("恶意书名不会写出目录", all(("/" not in n and "\\" not in n and ".." not in n) for n in names), names)
        check("恶意书名的那本还是下了（改成安全名字）", res3["downloaded"] == 1, res3)
        parent_names = os.listdir(root)
        check("没在上级目录留东西", all("坑人" not in n for n in parent_names), parent_names)
        bad_client = se.SyncClient(srv.base_url, "bad")
        err3 = None
        try:
            bad_client.blob_list()
        except Exception as e:
            err3 = e
        check("token 错了列不出来 → Unauthorized", isinstance(err3, se.Unauthorized), repr(err3))
        bad_client.close()
        client.close()

        section("幂等：没改动也能跑")
        ok = True
        for _ in range(2):
            try:
                A.round("noop")
                B.round("noop")
            except Exception:
                ok = False
                traceback.print_exc()
        check("空跑两轮不报错", ok)
        check("两边的账本秒数一致",
              abs(sum(s["sec"] for s in A.stats["sessions"]) - sum(s["sec"] for s in B.stats["sessions"])) < 1,
              (sum(s["sec"] for s in A.stats["sessions"]), sum(s["sec"] for s in B.stats["sessions"])))
    finally:
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)


def main(argv):
    t0 = time.time()
    if "--pure" not in argv:
        run_e2e()
    run_pure()
    print("\n%d 通过 / %d 失败（%.1f 秒）" % (PASS, FAIL, time.time() - t0))
    for b in BAD:
        print("  - " + b)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
