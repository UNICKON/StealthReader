# -*- coding: utf-8 -*-
"""生成 hupu_boards.py：从 bbs.hupu.com 首页的 `window.$$data` 里把板块表抠出来。

首页那一坨结构化数据长这样（实测 2026-10-19）：
    {"env":"prod", ..., "pageData":{"categories":[
        {"cateId":"10","name":"综合体育","url":"/all-sports",
         "hot":[{"topicId":"246","cateId":"10","name":"乒乓球区","url":"/pingpang",
                 "count":128321,"countText":"12.8w"}, ...]}, ...]}}
`/all-*` 是分类的聚合页（里面一条帖子都没有），所以只收 topics 里的板。

跑法（联网一次，把表重新生成）：
    python tests/gen_hupu_boards.py
"""

import io
import json
import os
import re
import sys

SR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SR, "libs"))
sys.path.insert(0, SR)

import feed_net

HOME = "https://bbs.hupu.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_home():
    cache = feed_net.Cache(os.path.join(SR, "cache", "feed"))
    http = feed_net.Http(cache=cache, headers={"User-Agent": UA, "Referer": HOME})
    raw = http.get_bytes(HOME, ttl=1, max_bytes=1500000, timeout=25)
    return raw.decode("utf-8", "replace")


def parse(html):
    m = re.search(r"window\.\$\$data\s*=\s*(\{.*?\});?\s*</script>", html, re.S)
    if not m:
        raise SystemExit("首页里没找到 window.$$data（改版了？）")
    data = json.loads(m.group(1))
    page = data.get("pageData") or {}
    # 分类表：cateId -> 分类名（categories 里每个分类自带的 hot 只是"热门几只"，
    # 真正全的表在页面更深处，所以先把 cateId 的名字收好，再全树扫一遍 topic 对象）
    cate_name = {}
    for cate in page.get("categories") or []:
        cid = str(cate.get("cateId") or "")
        if cid:
            cate_name[cid] = str(cate.get("name") or "").strip()
    found = {}

    def walk(node):
        if isinstance(node, dict):
            url = node.get("url")
            name = node.get("name")
            if (isinstance(url, str) and isinstance(name, str)
                    and node.get("topicId") and node.get("cateId")):
                slug = url.strip().lstrip("/").split("?")[0]
                if slug and not slug.startswith("all-") and "/" not in slug:
                    found.setdefault(slug, (
                        cate_name.get(str(node.get("cateId")), "虎扑"),
                        name.strip(), slug, int(node.get("count") or 0),
                        str(node.get("countText") or "")))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return list(found.values())


def write(rows):
    lines = []
    lines.append("# -*- coding: utf-8 -*-")
    lines.append('"""虎扑板块表（**生成物，别手改**）。')
    lines.append("")
    lines.append("数据来源：`https://bbs.hupu.com/` 首页里那段 `window.$$data` → `pageData.categories`。")
    lines.append("每行 `(分类, 板名, URL 里的 slug, 帖子总数, 热度文本)`；`/all-*` 是分类聚合页")
    lines.append("（里面一条帖子都没有），已经不在这张表里。要更新就重跑 `tests/gen_hupu_boards.py`。")
    lines.append('"""')
    lines.append("")
    lines.append("GENERATED_AT = \"%s\"" % __import__("datetime").date.today().isoformat())
    lines.append("")
    lines.append("CATALOG = (")
    for group, name, slug, count, ctext in rows:
        lines.append("    (%r, %r, %r, %d, %r)," % (group, name, slug, count, ctext))
    lines.append(")")
    lines.append("")
    path = os.path.join(SR, "hupu_boards.py")
    io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    return path


if __name__ == "__main__":
    rows = parse(fetch_home())
    path = write(rows)
    alive = [r for r in rows if r[3] > 0]
    print("板块 %d 个（其中有帖子的 %d 个）→ %s" % (len(rows), len(alive), path))
    for r in rows[:5]:
        print("   ", r)
