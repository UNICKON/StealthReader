# -*- coding: utf-8 -*-
"""录制真实响应当夹具（**故意联网**，只在需要更新夹具时手动跑一次）。

探针是这一批功能的验收依据，而探针跑在夹具上；夹具必须是**真响应**，不能手写——
手写的夹具只能证明"代码符合我的想象"，证明不了"代码符合那个站"。

    python tests/record_fixtures.py            # 全部录一遍，能录到的才写盘
    python tests/record_fixtures.py zhihu      # 只录某一个

录不到的不报错、也不写空文件：那个站的探针章节自己会跳过（比如 V2EX 在本机网络连不上，
linux.do 的盾时开时关），夹具目录里缺哪个文件就是"这次没录到"。
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1")

S = requests.Session()


def save(name, url, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    try:
        r = S.get(url, headers=h, timeout=20)
    except Exception as e:
        print("  跳过 %-24s %s: %s" % (name, type(e).__name__, e))
        return None
    if r.status_code != 200 or len(r.content) < 200:
        print("  跳过 %-24s HTTP %d / %d 字节" % (name, r.status_code, len(r.content)))
        return None
    path = os.path.join(OUT, name)
    with open(path, "wb") as f:
        f.write(r.content)
    print("  录到 %-24s %6d 字节" % (name, len(r.content)))
    try:
        return json.loads(r.content.decode("utf-8", "replace"))
    except Exception:
        return None


def _pick_topic(latest, least_posts=4):
    """挑一个**有回复**的主题录下来：没有回复就验不了"回复树"这条重点。

    Discourse 的 latest.json 排在最前的常是置顶公告（只有一个楼主帖），
    所以按 posts_count 挑最活跃的那个；挑不到就退回第一条。"""
    try:
        topics = ((latest or {}).get("topic_list") or {}).get("topics") or []
    except Exception:
        return ""
    cand = [t for t in topics if int(t.get("posts_count") or 0) >= least_posts]
    pick = (cand or topics)[:1]
    return str(pick[0].get("id") or "") if pick else ""


AUTH = os.path.join(os.path.dirname(HERE), "feed_auth.json")


def auth_for(key):
    """从 feed_auth.json 取某个站的凭据（没有就返回 None，调用方跳过）。"""
    try:
        with open(AUTH, "r", encoding="utf-8") as f:
            node = (json.load(f) or {}).get(key)
    except Exception:
        return None
    return node if isinstance(node, dict) and node.get("cookie") else None


def save_authed(name, url, cred, headers=None, trim=None):
    """带 Cookie 录一次，**存之前先交给 trim 裁一遍**。

    带 Cookie 拿到的响应里有作者 id / 头像地址 / 整篇正文，直接塞进公开仓库不合适；
    裁成"适配器真正用到的那几个字段"就够了——夹具要证明的是解析对得上，不是把原文搬回家。"""
    h = {"User-Agent": cred.get("user_agent") or UA, "Accept": "*/*",
         "Cookie": cred.get("cookie") or ""}
    h.update(headers or {})
    try:
        r = S.get(url, headers=h, timeout=25)
    except Exception as e:
        print("  跳过 %-24s %s: %s" % (name, type(e).__name__, e))
        return None
    if r.status_code != 200:
        print("  跳过 %-24s HTTP %d（Cookie 过期了？）" % (name, r.status_code))
        return None
    try:
        obj = json.loads(r.content.decode("utf-8", "replace"))
    except Exception:
        print("  跳过 %-24s 不是 JSON" % name)
        return None
    if trim:
        obj = trim(obj)
    body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(body)
    print("  录到 %-24s %6d 字节（已裁剪）" % (name, len(body)))
    return obj


def fetch_raw(url, cred, headers=None):
    """带 Cookie 取一次**原始字节**，返回 `(bytes, HTTP 状态码)`。不落盘、不抛。

    NGA 这种站要拿原始字节：它的 JSON 里有裸控制字符、编码还自称 gbk，
    用 `r.json()` 会直接报错，所以这里只负责取回来。"""
    h = {"User-Agent": cred.get("user_agent") or UA, "Accept": "*/*",
         "Cookie": cred.get("cookie") or ""}
    h.update(headers or {})
    try:
        r = S.get(url, headers=h, timeout=25)
    except Exception as e:
        print("  取不到 %s：%s: %s" % (url[:64], type(e).__name__, e))
        return None, 0
    return r.content, r.status_code


def fetch_json(url, cred, headers=None, params=None, payload=None):
    """带 Cookie 取一次 JSON（`payload` 给了就走 POST）。取不到/不是 JSON 都返回 None。"""
    h = {"User-Agent": cred.get("user_agent") or UA, "Accept": "application/json, */*",
         "Cookie": cred.get("cookie") or ""}
    h.update(headers or {})
    try:
        if payload is None:
            r = S.get(url, headers=h, params=params, timeout=25)
        else:
            r = S.post(url, headers=h, json=payload, timeout=25)
    except Exception as e:
        print("  取不到 %s：%s: %s" % (url[:64], type(e).__name__, e))
        return None
    if r.status_code != 200:
        print("  %s HTTP %d" % (url[:64], r.status_code))
        return None
    try:
        return r.json()
    except Exception as e:
        print("  %s 不是 JSON：%s" % (url[:64], e))
        return None


def save_raw(name, obj, note=""):
    """把**已经裁过**的对象存成夹具 JSON。"""
    body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(body)
    print("  录到 %-24s %6d 字节%s" % (name, len(body), ("（%s）" % note) if note else ""))
    return obj


def write_bytes(name, data):
    """原样存字节：NGA 那条 403 是 GBK，转码反而容易失真。"""
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(data)
    return data


def _name(a):
    if isinstance(a, dict):
        if a.get("name"):
            return str(a["name"])
        m = a.get("member")
        if isinstance(m, dict) and m.get("name"):
            return str(m["name"])
    return ""


def _trim_answer(obj):
    """只留**够验形状的一小段**：第一段正文 + 元数据。

    知乎的正文和评论是别人写的话，不是我的代码产物。夹具要证明的是"解析对得上真响应"，
    不是把别人的文章搬进公开仓库，所以这里只留第一段（标签结构、`data-pid` 都还在），
    长文一律截掉。"""
    data = (obj or {}).get("data") or []
    if not data:
        return obj
    a = data[0] if isinstance(data[0], dict) else {}
    content = str(a.get("content") or "")
    first = content.split("</p>")[0] + "</p>" if "</p>" in content else content
    return {"data": [{
        "id": a.get("id"), "type": a.get("type"),
        "content": first,
        "excerpt": (a.get("excerpt") or "")[:60],
        "voteup_count": a.get("voteup_count"), "comment_count": a.get("comment_count"),
        "created_time": a.get("created_time"), "updated_time": a.get("updated_time"),
        "author": {"name": _name(a.get("author"))},
    }]}


def _trim_comments(obj, keep=3, each=40):
    """同理：只留前几条、每条一小截，够验"作者嵌在 author.member.name"就行。"""
    out = []
    for c in ((obj or {}).get("data") or [])[:keep]:
        if not isinstance(c, dict):
            continue
        au = c.get("author")
        keep_author = None
        if isinstance(au, dict):
            # 保留"作者可能整个是 null"这个真实形状：第一版代码按 author.name 取，
            # 拿到的是空字符串——这条夹具就是拿来盯这个的。
            keep_author = {"role": au.get("role"),
                           "member": ({"name": _name(au)} if _name(au) else None)}
        out.append({"id": c.get("id"), "content": (c.get("content") or "")[:each],
                    "vote_count": c.get("vote_count"), "created_time": c.get("created_time"),
                    "author": keep_author})
    return {"data": out, "_trimmed": "只留了前 %d 条、每条前 %d 字（别人的原话不整段搬进仓库）"
            % (keep, each)}


def _pick_question(hot):
    """热榜里挑第一条有真问题 id 的（知乎热榜的 target 就是问题）。"""
    for it in (hot or {}).get("data") or []:
        t = (it or {}).get("target") or {}
        if t.get("id"):
            return str(t["id"])
    return ""


def _nga_load(raw):
    return json.loads(raw.decode("gbk", "replace"), strict=False)


def _nga_pick_list(obj):
    data = (obj or {}).get("data") or {}
    block = data.get("__T") if isinstance(data.get("__T"), dict) else {}
    try:
        keys = sorted(block.keys(), key=lambda x: int(x))
    except Exception:
        keys = list(block.keys())
    return [(k, block[k]) for k in keys if isinstance(block.get(k), dict)]


def _trim_nga_list(obj, keep=6):
    """列表只留几条 + 只留适配器用到的字段（`__U`/`__GLOBAL`/`__CU` 这些一概不留）。"""
    out = {}
    for k, it in _nga_pick_list(obj)[:keep]:
        out[k] = {f: it.get(f) for f in ("tid", "subject", "author", "postdate", "lastpost",
                                         "replies", "type", "fid")}
    return {"data": {"__T": out}}


def _trim_nga_category(obj, groups=1, each=8):
    """板块分类树：每个分类只留前 1 组、每组前 8 条（真响应里有 7 个分类 / 661 个板块）。

    只留解析器用到的 `fid` / `name` / `info` / `is_forumlist` / `id` / `icon`，
    外层的 `forum_icon_pre` / `appcolumn_notis` / `buff` 这些广告和图标前缀一概不留。
    形状（含"fid 有时候是整数、有时候是字符串"这个坑）照原样保留。"""
    out = []
    for cat in (obj or {}).get("result") or []:
        gs = []
        for g in (cat.get("groups") or [])[:groups]:
            fs = [{k: f.get(k) for k in ("fid", "name", "info", "is_forumlist", "id", "icon")}
                  for f in (g.get("forums") or [])[:each]]
            gs.append({"name": g.get("name"), "id": g.get("id"), "forums": fs})
        out.append({"id": cat.get("id"), "_id": cat.get("_id"), "name": cat.get("name"),
                    "groups": gs})
    return {"code": (obj or {}).get("code"), "result": out}


def _trim_nga_read(obj, keep=3, each=220):
    """正文只留前几楼、每楼前 220 字；用户表只留 `uid` + `username`。

    `__U` 里每个人都带着 credit / money / postnum 这些资料，没必要进公开仓库，
    而夹具要验的只有"作者的 uid 能不能对上用户名"。"""
    data = (obj or {}).get("data") or {}
    meta = data.get("__T") if isinstance(data.get("__T"), dict) else {}
    keep_meta = {f: meta.get(f) for f in ("subject", "author", "fid", "lastpost", "postdate")}
    rows, ids = {}, []
    block = data.get("__R") if isinstance(data.get("__R"), dict) else {}
    try:
        keys = sorted(block.keys(), key=lambda x: int(x))
    except Exception:
        keys = list(block.keys())
    for k in keys[:keep]:
        it = block[k]
        if not isinstance(it, dict):
            continue
        rows[k] = {"lou": it.get("lou"), "pid": it.get("pid"), "authorid": it.get("authorid"),
                   "postdate": it.get("postdate"),
                   "postdatetimestamp": it.get("postdatetimestamp"),
                   "score": it.get("score"), "subject": it.get("subject"),
                   "content": (it.get("content") or "")[:each]}
        if it.get("authorid") is not None:
            ids.append(str(it["authorid"]))
    users = data.get("__U") if isinstance(data.get("__U"), dict) else {}
    return {"data": {"__T": keep_meta, "__R": rows, "__ROWS": data.get("__ROWS"),
                     "__U": {i: {"uid": (users.get(i) or {}).get("uid"),
                                 "username": (users.get(i) or {}).get("username")}
                             for i in ids if isinstance(users.get(i), dict)}}}


def _xhs_state(html):
    """跟适配器同一套抠法（这里只为录夹具，逻辑保持一致）。"""
    at = str(html or "").find("window.__INITIAL_STATE__")
    at = html.find("{", at)
    depth, i, n, in_str, quote, esc = 0, at, len(html), False, "", False
    while i < n:
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str, quote = True, ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(re.sub(r"\bundefined\b", "null", html[at:i + 1])
                                  .replace("new Map([])", "{}").replace("new Set([])", "[]"))
        i += 1
    return None


def _trim_xhs_explore(state, keep=3):
    """列表状态：只留 `feed.feeds` 的前几条，每条只留适配器用到的字段。"""
    feeds = ((state or {}).get("feed") or {}).get("feeds") or []
    out = []
    for it in feeds:
        if not isinstance(it, dict):
            continue
        card = it.get("noteCard") or {}
        if not isinstance(card, dict) or not str(card.get("displayTitle") or "").strip():
            continue
        out.append({
            "id": it.get("id"), "xsecToken": it.get("xsecToken"),
            "noteCard": {
                "type": card.get("type"),
                "displayTitle": card.get("displayTitle"),
                "user": {"nickName": (card.get("user") or {}).get("nickName")},
                "interactInfo": {"likedCount": (card.get("interactInfo") or {}).get("likedCount")},
                "cover": {"infoList": [{"url": u} for u in
                                       [x.get("url") for x in ((card.get("cover") or {}).get("infoList") or [])[:1]
                                        if isinstance(x, dict)]]},
            },
        })
        if len(out) >= keep:
            break
    return {"feed": {"feeds": out}}


def _trim_xhs_note(state, nid, desc_limit=400, imgs=2):
    """详情状态：正文只留前 400 字、图只留前 2 张、去掉 ipLocation 这类个人信息。"""
    node = ((state or {}).get("note") or {}).get("noteDetailMap") or {}
    one = node.get(nid) if isinstance(node, dict) else None
    if not isinstance(one, dict):
        return None
    note = one.get("note") or {}
    if not isinstance(note, dict):
        return None
    info = note.get("interactInfo") or {}
    keep = {"noteId": note.get("noteId"), "type": note.get("type"),
            "title": note.get("title"), "desc": (note.get("desc") or "")[:desc_limit],
            "time": note.get("time"),
            "user": {"nickname": (note.get("user") or {}).get("nickname")},
            "interactInfo": {"likedCount": info.get("likedCount"),
                             "commentCount": info.get("commentCount")},
            "tagList": [{"name": t.get("name")} for t in (note.get("tagList") or [])
                        if isinstance(t, dict) and t.get("name")],
            "imageList": [{"urlDefault": im.get("urlDefault") or im.get("url")}
                          for im in (note.get("imageList") or [])[:imgs] if isinstance(im, dict)]}
    raw = ((one.get("comments") or {}).get("list") or [])
    comments = []
    for c in raw[:3]:
        if not isinstance(c, dict):
            continue
        subs = []
        for sc in (c.get("subComments") or [])[:2]:
            if isinstance(sc, dict):
                subs.append({"id": sc.get("id"), "content": (sc.get("content") or "")[:80],
                             "likeCount": sc.get("likeCount"), "createTime": sc.get("createTime"),
                             "userInfo": {"nickname": (sc.get("userInfo") or {}).get("nickname")}})
        comments.append({"id": c.get("id"), "content": (c.get("content") or "")[:120],
                         "likeCount": c.get("likeCount"), "createTime": c.get("createTime"),
                         "userInfo": {"nickname": (c.get("userInfo") or {}).get("nickname")},
                         "subComments": subs})
    return {"note": {"noteDetailMap": {nid: {
        "note": keep,
        "comments": {"list": comments, "hasMore": bool((one.get("comments") or {}).get("hasMore"))},
    }}}}


def _trim_weread_shelf(shelf, keep=3):
    """书架：只留 3 本 + 适配器用到的字段；`bookProgress` 只留这几本的。

    挑的时候优先挑**有进度**的（0% 的书记不到"读到哪了"这层重点）。"""
    books = [b for b in (shelf.get("books") or []) if isinstance(b, dict) and b.get("bookId")]
    prog = {str(p.get("bookId")): p for p in (shelf.get("bookProgress") or [])
            if isinstance(p, dict) and p.get("bookId")}
    with_prog = [b for b in books if str(b["bookId"]) in prog]
    pick = (with_prog + books)[:keep]
    ids = [str(b["bookId"]) for b in pick]
    out_books = []
    for b in pick:
        out_books.append({
            "bookId": str(b["bookId"]), "title": b.get("title"), "author": b.get("author"),
            "cover": b.get("cover"), "deepLink": b.get("deepLink"),
            "finishReading": b.get("finishReading"), "readUpdateTime": b.get("readUpdateTime"),
        })
    out_prog = []
    for bid in ids:
        p = prog.get(bid)
        if not p:
            continue
        out_prog.append({k: p.get(k) for k in
                         ("bookId", "progress", "chapterUid", "chapterIdx", "readingTime",
                          "updateTime", "synckey")})
    return {"books": out_books, "bookProgress": out_prog}


def _trim_weread_chapters(chs, book_id, keep=3, cur_uid=0):
    """章节表：留前 3 章 **+ 当前读到的那一章**，其余字段只留适配器用到的。

    为什么要专门把当前章塞进来：只看前 3 章的话，"目录里 ▶ 标出读到哪一章"这条就没法验
    （实测这本书读到第 10 章，前 3 章里根本没有它）。"""
    one = ((chs or {}).get("data") or [{}])
    one = one[0] if one and isinstance(one[0], dict) else {}
    ups = [c for c in (one.get("updated") or []) if isinstance(c, dict)]
    if not ups:
        return None
    pick = ups[:keep]
    if cur_uid:
        extra = [c for c in ups if int(c.get("chapterUid") or 0) == int(cur_uid)]
        if extra and all(int(c.get("chapterUid") or 0) != int(cur_uid) for c in pick):
            pick = pick + extra
    pick.sort(key=lambda c: int(c.get("chapterIdx") or 0))
    return {"data": [{"bookId": str(book_id), "updated": [
        {k: c.get(k) for k in ("chapterUid", "chapterIdx", "title", "wordCount", "paid", "level")}
        for c in pick]}]}


def _trim_weread_progress(pr, limit=60):
    """进度：`summary` 是**书里的原句**，只留前 60 字（跟知乎正文、小红书正文一个规矩）。"""
    book = (pr or {}).get("book")
    if not isinstance(book, dict):
        return None
    keep = {k: book.get(k) for k in
            ("bookId", "progress", "chapterUid", "chapterIdx", "readingTime", "updateTime")}
    if book.get("summary"):
        keep["summary"] = str(book["summary"])[:limit]
    return {"bookId": str((pr or {}).get("bookId") or book.get("bookId") or ""), "book": keep}


def record(which):
    if which in ("", "ithome"):
        print("IT之家")
        hot = save("ithome_hot.json", "https://m.ithome.com/api/news/newslistpageget?Tag=hot&Page=0")
        save("ithome_newslist.json", "https://api.ithome.com/json/newslist/news")
        if isinstance(hot, dict):
            items = hot.get("Result") or []
            nid = next((str(it.get("newsid")) for it in items if it.get("newsid")), "")
            if nid:
                save("ithome_detail.json",
                     "https://api.ithome.com/json/newscontent/%s" % nid)
    if which in ("", "linuxdo"):
        print("linux.do（Cloudflare 盾时开时关，录不到就跳过）")
        latest = save("discourse_latest.json", "https://linux.do/latest.json")
        tid = _pick_topic(latest)
        if tid:
            save("discourse_topic.json", "https://linux.do/t/%s.json" % tid)
        else:
            # linux.do 被盾拦着也得有 Discourse 夹具，否则适配器（回复树、最后回复时间）
            # 就没人盯了。meta.discourse.org 是官方站，结构完全一样——用它录，
            # 并在 tests/README.md 里写明这两个文件是从哪来的。
            print("  退回 meta.discourse.org（官方 Discourse，结构相同）")
            latest = save("discourse_latest.json", "https://meta.discourse.org/latest.json")
            tid = _pick_topic(latest)
            if tid:
                save("discourse_topic.json", "https://meta.discourse.org/t/%s.json" % tid)
    if which in ("", "zhihu"):
        print("知乎")
        hot = save("zhihu_hot_list.json",
                   "https://api.zhihu.com/topstory/hot-list?limit=50&reverse_order=0",
                   headers={"Referer": "https://www.zhihu.com/"})
        cred = auth_for("zhihu")
        if not cred:
            print("  正文/评论要 Cookie（feed_auth.json 里没有），跳过——"
                  "夹具里那个 zhihu_unauth_answers.json 就是「没配」时的真响应")
        else:
            qid = _pick_question(hot)
            if not qid:
                print("  热榜里没挑到问题 id，跳过正文")
            else:
                ans = save_authed(
                    "zhihu_answer.json",
                    "https://www.zhihu.com/api/v4/questions/%s/answers"
                    "?include=data[*].content,excerpt,voteup_count,comment_count&limit=1&offset=0" % qid,
                    cred, headers={"Referer": "https://www.zhihu.com/question/%s" % qid,
                                   "x-requested-with": "fetch"}, trim=_trim_answer)
                aid = ""
                if isinstance(ans, dict) and ans.get("data"):
                    aid = str((ans["data"][0] or {}).get("id") or "")
                if aid:
                    save_authed(
                        "zhihu_comments.json",
                        "https://www.zhihu.com/api/v4/answers/%s/comments"
                        "?limit=20&offset=0&order=normal&status=open" % aid,
                        cred, headers={"Referer": "https://www.zhihu.com/",
                                       "x-requested-with": "fetch"}, trim=_trim_comments)
    if which in ("", "nga"):
        print("NGA")
        cred = auth_for("nga")
        if not cred:
            print("  列表/正文都要 Cookie（feed_auth.json 里没有），跳过")
        else:
            ref = {"Referer": "https://bbs.nga.cn/"}
            raw, status = fetch_raw("https://bbs.nga.cn/thread.php?fid=-7&__output=8", cred, ref)
            lst = None
            if raw is None or status != 200:
                print("  列表没拿到（HTTP %s），跳过" % status)
            else:
                try:
                    lst = _nga_load(raw)
                except Exception as e:
                    print("  列表不是能读的 JSON（%s），跳过" % e)
            rows = _nga_pick_list(lst) if lst is not None else []
            if rows:
                save_raw("nga_list.json", _trim_nga_list(lst), note="已裁剪：只留 6 条 + 适配器用到的字段")
                # 挑最新的（`lastpost` 最大）录正文——老的会被 NGA 用"时间超过限制"挡掉
                newest = sorted(rows, key=lambda kv: int((kv[1] or {}).get("lastpost") or 0))[-1][1]
                tid = str(newest.get("tid") or "")
                if tid:
                    raw2, status = fetch_raw(
                        "https://bbs.nga.cn/read.php?tid=%s&__output=8" % tid, cred, ref)
                    if status == 200 and raw2:
                        try:
                            save_raw("nga_read.json",
                                     _trim_nga_read(_nga_load(raw2)),
                                     note="已裁剪：%s 的前 3 楼、每楼 220 字；用户表只留 uid+username" % tid)
                        except Exception as e:
                            print("  正文 JSON 读不出来（%s），跳过" % e)
                    else:
                        print("  正文这条没拿到（HTTP %s）" % status)
                # 再挑最老的一条：NGA 会回 403「5:帖子发布或回复时间超过限制」——
                # 这条真响应专门用来验"别把权限问题谎报成登录问题"
                oldest = sorted(rows, key=lambda kv: int((kv[1] or {}).get("lastpost") or 0))[0][1]
                otid = str(oldest.get("tid") or "")
                if otid:
                    raw3, status3 = fetch_raw(
                        "https://bbs.nga.cn/read.php?tid=%s&__output=8" % otid, cred, ref)
                    if status3 == 403 and raw3 and "时间超过限制".encode("gbk") in raw3:
                        write_bytes("nga_old_thread_403.json", raw3)
                        print("  录到 nga_old_thread_403.json   %8d 字节（原样，GBK）" % len(raw3))
                    else:
                        print("  老帖这次不是 403（HTTP %s），没录那条" % status3)
        # 板块分类树：**不要 Cookie**（跟上面两条不一样），所以放在 `if cred` 外面
        # （`fetch_raw` 要一个 cred 字典，空字典就行：它会退回默认 UA、不带 Cookie）
        raw4, status4 = fetch_raw(
            "https://bbs.nga.cn/app_api.php?__lib=home&__act=category&_v=2", {},
            {"Referer": "https://bbs.nga.cn/"})
        if status4 == 200 and raw4:
            try:
                cat = json.loads(raw4.decode("utf-8", "replace"))
            except Exception as e:
                print("  板块分类树不是能读的 JSON（%s），跳过" % e)
            else:
                save_raw("nga_category.json", _trim_nga_category(cat),
                         note="已裁剪：7 个分类各留第一组前 8 条（真响应里有 661 个板块）")
        else:
            print("  板块分类树没拿到（HTTP %s）" % status4)
    if which in ("", "xhs"):
        print("小红书")
        cred = auth_for("xhs")
        if not cred:
            print("  免登录只有空壳，跳过")
        else:
            ref = {"Referer": "https://www.xiaohongshu.com/",
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
            raw, status = fetch_raw("https://www.xiaohongshu.com/explore", cred, ref)
            state = None
            if raw is None or status != 200:
                print("  探索页没拿到（HTTP %s），跳过" % status)
            else:
                try:
                    state = _xhs_state(raw.decode("utf-8", "replace"))
                except Exception as e:
                    print("  状态解不开（%s），跳过" % e)
            feeds = ((state or {}).get("feed") or {}).get("feeds") or []
            if not feeds:
                print("  探索页里没有 feeds（Cookie 过期了？），跳过")
            else:
                save_raw("xhs_explore_state.json", _trim_xhs_explore(state),
                         note="已裁剪：只留 3 条 + 适配器用到的字段")
                # 详情页自带几条评论就录几条——实测有时 5 条、有时 0 条（评论是滚动加载的），
                # 所以多试两条笔记，尽量录到"有评论"的那份，好把评论解析也钉住。
                notes = [it for it in feeds
                         if isinstance(it, dict) and str((it.get("noteCard") or {}).get("type")) != "video"]
                best = None
                for it in notes[:10]:
                    nid = str(it.get("id") or "")
                    token = str(it.get("xsecToken") or "")
                    if not nid or not token:
                        continue
                    u = ("https://www.xiaohongshu.com/explore/%s?xsec_token=%s&xsec_source=pc_feed"
                         % (nid, quote(token, safe="")))
                    raw2, st2 = fetch_raw(u, cred, ref)
                    if raw2 is None or st2 != 200:
                        print("  详情 %s 没拿到（HTTP %s）" % (nid[:8], st2))
                        continue
                    try:
                        s2 = _xhs_state(raw2.decode("utf-8", "replace"))
                    except Exception as e:
                        print("  详情状态解不开（%s）" % e)
                        continue
                    one = ((s2.get("note") or {}).get("noteDetailMap") or {}).get(nid) or {}
                    got = len(((one.get("comments") or {}).get("list") or []))
                    print("  详情 %s… 自带 %d 条评论" % (nid[:8], got))
                    if got:
                        best = (s2, nid)
                        break
                    if best is None:
                        best = (s2, nid)
                if best:
                    trimmed = _trim_xhs_note(best[0], best[1])
                    if trimmed:
                        save_raw("xhs_note_state.json", trimmed,
                                 note="已裁剪：正文前 400 字、图前 2 张、去掉 ipLocation")
                    else:
                        print("  详情状态里没有这条笔记，没录")
    if which in ("", "hupu"):
        print("虎扑（免登录：版面页是 HTML，帖子页的数据在 __NEXT_DATA__ 里）")
        hp_head = {"User-Agent": UA, "Referer": "https://bbs.hupu.com/"}
        try:
            hp_raw, _code = fetch_raw("https://bbs.hupu.com/bxj", {}, hp_head)
            if not hp_raw:
                raise RuntimeError("空响应")
            hp_html = hp_raw.decode("utf-8", "replace")
        except Exception as e:
            print("  版面页没拿到（%s）" % e)
        else:
            items = re.findall(r'<li class="bbs-sl-web-post-body".*?</li>', hp_html, re.S)[:12]
            shell = '<html><body><div class="bbs-sl-web-post"><ul>'
            write_bytes("hupu_list.html",
                        (shell + "\n" + "\n".join(items) + "\n</ul></div></body></html>\n").encode("utf-8"))
            print("  版面列表 %d 条" % len(items))
            # 清单里不少是纯图/视频帖，正文一个字都没有——那种当不了夹具，要挑有字的
            picked = None
            for tid in re.findall(r'href="/(\d+)\.html"', "\n".join(items))[:10]:
                try:
                    page_raw, _c2 = fetch_raw("https://bbs.hupu.com/%s.html" % tid, {}, hp_head)
                    if not page_raw:
                        continue
                    page = page_raw.decode("utf-8", "replace")
                except Exception:
                    continue
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
                if not m:
                    continue
                try:
                    detail = json.loads(m.group(1))["props"]["pageProps"]["detail"]
                except Exception:
                    continue
                text = re.sub(r"<[^>]+>", "", (detail.get("thread") or {}).get("content") or "")
                if len(re.sub(r"\s+", "", text)) >= 200:
                    picked = (tid, detail)
                    break
            if not picked:
                print("  前十篇里没有带文字正文的帖子，帖子夹具没录")
            else:
                tid, detail = picked
                thread = {k: (detail.get("thread") or {}).get(k) for k in (
                    "tid", "title", "content", "video", "videoCover", "lights", "replies",
                    "read", "createdAt", "createdAtFormat", "author")}
                thread["content"] = re.sub(r' data-origin="[^"]*"', "", thread.get("content") or "")
                reps = [{k: it.get(k) for k in ("pid", "content", "allLightCount", "createdAt",
                                                "createdAtFormat", "author", "replyNum")}
                        for it in (((detail.get("replies") or {}).get("list")) or [])[:6]
                        if isinstance(it, dict)]
                trim = {"thread": thread, "replies": {"count": len(reps), "list": reps},
                        "breadCrumb": detail.get("breadCrumb")}
                page_html = ('<html><head><title>%s</title></head><body>'
                             '<script id="__NEXT_DATA__" type="application/json">%s</script>'
                             '</body></html>\n') % (
                                 thread.get("title") or "",
                                 json.dumps({"props": {"pageProps": {"detail": trim}}},
                                            ensure_ascii=False))
                write_bytes("hupu_thread.html", page_html.encode("utf-8"))
                print("  帖子夹具 tid=%s、正文 %d 字、回复 %d 条" % (tid, len(thread["content"]), len(reps)))
    if which in ("", "weibo"):
        print("微博")
        save("weibo_hotsearch.json", "https://weibo.com/ajax/side/hotSearch",
             headers={"Referer": "https://weibo.com/", "User-Agent": MOBILE_UA})
    if which in ("", "douban"):
        print("豆瓣（讨论精选免登录；话题正文/回复要登录）")
        save("douban_explore.html", "https://www.douban.com/group/explore")
        tid = ""
        try:
            with open(os.path.join(OUT, "douban_explore.html"), "r", encoding="utf-8") as f:
                hit = re.search(r"/group/topic/(\d+)/", f.read())
            tid = hit.group(1) if hit else ""
        except Exception:
            tid = ""
        cred = auth_for("douban") or {}
        if not tid:
            print("  讨论精选里没挑到话题 id，跳过话题页")
        else:
            raw2, status = fetch_raw("https://www.douban.com/group/topic/%s/" % tid, cred,
                                     {"Referer": "https://www.douban.com/group/"})
            _t2 = (raw2 or b"").decode("utf-8", "replace")
            if status == 200 and raw2 and "禁止访问" in _t2:
                # 风控页也是 200（1215 字节 + 腾讯验证码），别拿它盖掉真话题页
                write_bytes("douban_blocked.html", raw2)
                print("  豆瓣把这次请求挡了（禁止访问/验证码），录成 douban_blocked.html，"
                      "没动 douban_topic.html——过几分钟再录")
            elif status == 200 and raw2 and "topic-content" not in _t2 and "link-report" not in _t2:
                print("  话题页 200 但里面没正文（%d 字节），不覆盖夹具" % len(raw2))
            elif status == 200 and raw2:
                write_bytes("douban_topic.html", raw2)
                print("  录到 douban_topic.html   %8d 字节（带 Cookie）" % len(raw2))
            elif status == 403 and raw2:
                # 匿名一律 403：这条真响应专门用来验「照实说要登录」（别谎报成「取数失败」）
                write_bytes("douban_topic_403.html", raw2)
                print("  录到 douban_topic_403.html   %8d 字节（匿名 403，真响应）" % len(raw2))
            else:
                print("  话题页没录到（HTTP %s）" % status)
    if which in ("", "v2ex"):
        print("V2EX（本机网络连不上是常态，连上才录）")
        save("v2ex_hot.json", "https://www.v2ex.com/api/topics/hot.json")
    if which in ("", "weread"):
        print("微信读书（要 Cookie）")
        cred = auth_for("weread")
        if not cred:
            print("  没配 Cookie，跳过")
        else:
            ref = {"Referer": "https://weread.qq.com/", "Origin": "https://weread.qq.com"}
            shelf = fetch_json("https://weread.qq.com/web/shelf/sync", cred, ref)
            if not isinstance(shelf, dict) or not (shelf.get("books") or []):
                print("  书架没拿到（Cookie 过期了？），跳过")
            else:
                trimmed_shelf = _trim_weread_shelf(shelf)
                save_raw("weread_shelf.json", trimmed_shelf,
                         note="已裁剪：只留 3 本 + 适配器用到的字段")
                # 章节表 / 进度录的是**书架上第一本**（不是随手挑一本）：三份夹具要能互相对上，
                # 否则探针里"章节里有当前章"这种检查根本没法验（第一版就是各录各的，对不上）。
                target = str((trimmed_shelf["books"] or [{}])[0].get("bookId") or "")
                cur_uid = 0
                for p in (trimmed_shelf.get("bookProgress") or []):
                    if str(p.get("bookId")) == target:
                        cur_uid = int(p.get("chapterUid") or 0)
                chs = fetch_json("https://weread.qq.com/web/book/chapterInfos", cred, ref,
                                 payload={"bookIds": [target]})
                trimmed = _trim_weread_chapters(chs, target, cur_uid=cur_uid)
                if trimmed:
                    save_raw("weread_chapters.json", trimmed,
                             note="已裁剪：前 3 章 + 当前读到的那一章")
                else:
                    print("  章节表没拿到，没录")
                pr = fetch_json("https://weread.qq.com/web/book/getProgress", cred, ref,
                                params={"bookId": target})
                trimmed = _trim_weread_progress(pr)
                if trimmed:
                    save_raw("weread_progress.json", trimmed,
                             note="已裁剪：summary 只留前 60 字（那是书里的原句）")
                else:
                    print("  进度没拿到，没录")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    want = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    t0 = time.time()
    record(want)
    files = sorted(os.listdir(OUT))
    print("\n夹具目录 %s（%.1fs）: %s" % (OUT, time.time() - t0, ", ".join(files) or "空"))
