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
import sys
import time

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
    if which in ("", "weibo"):
        print("微博")
        save("weibo_hotsearch.json", "https://weibo.com/ajax/side/hotSearch",
             headers={"Referer": "https://weibo.com/", "User-Agent": MOBILE_UA})
    if which in ("", "v2ex"):
        print("V2EX（本机网络连不上是常态，连上才录）")
        save("v2ex_hot.json", "https://www.v2ex.com/api/topics/hot.json")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    want = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    t0 = time.time()
    record(want)
    files = sorted(os.listdir(OUT))
    print("\n夹具目录 %s（%.1fs）: %s" % (OUT, time.time() - t0, ", ".join(files) or "空"))
