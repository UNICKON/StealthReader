# -*- coding: utf-8 -*-
"""社区统一流的真实来源适配器（批次 B / D / E）。

每个来源只做三件事：列表 / 正文 / 评论，全部返回 feed.py 的 Post / Comment。
界面层一行都不用改——这就是批次 A 定下的契约。

当前来源：
- **IT之家**：免登录，本机实测可用（移动端接口 + 正文接口）；
- **V2EX**：免登录的公开 API，但**本机网络到不了**（TCP 443 超时，见 README），代码按官方
  文档写、用夹具验；网络恢复即可用；
- **linux.do**：标准 Discourse，站前挂着 Cloudflare 盾，**必须贴浏览器 Cookie**
  （含 cf_clearance，且 UA 要一致）才能过；
- **知乎**：免登录只能拿热榜（移动端 `api.zhihu.com/topstory/hot-list`），正文/评论要登录；
- **微博**：免登录只能拿热搜榜，正文/评论要登录（**默认关闭**，属于"单独开关"那一档）；
- **混流**：把上面几个合页成一条，"混流为主"就靠它。
另有 feed.py 里的 DemoSource（离线示例）垫底。

Cookie 从应用目录下的 feed_auth.json 读（已 gitignore），格式：
    {"linuxdo": {"cookie": "...", "user_agent": "..."}, "v2ex": {"cookie": "..."}}
批次 C 会把它换成 DPAPI 加密 + 设置界面，这里先按最小可用做。
"""

import json
import os
import re
import threading
import time
from urllib.parse import quote

import feed as feed_mod
from feed import Block, Comment, Post, SourceAdapter, BLOCK_IMG, BLOCK_P, BLOCK_QUOTE
import feed_net
from feed_net import FeedNetError, Http, TTL_LIST, TTL_DETAIL, TTL_FOREVER

PAGE_MIX = 12           # 混流每页几条
MAX_PAGES = 6           # 单个来源最多自动续几页（别把"快到底自动续页"变成无限请求）
LIST_TIMEOUT = 6        # 列表页的超时：比正文短，因为混流要等所有来源，一个卡住就整页卡住
COVER_PER_PAGE = 6      # 一页最多下几张封面：25 张图一起下会让"唤出社区页"等十几秒
FAIL_COOLDOWN = 60      # 某个来源刚失败过，就歇这么久再去碰它（别每翻一页都去撞一次南墙）


def load_auth(path):
    """读 feed_auth.json。读不到/格式不对都返回空字典（来源会退化成"未配置"，不炸）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class RealSource(SourceAdapter):
    """真实来源的公共部分：错误信息、图片目录、把异常收成一句人话。"""

    needs_auth = False
    base_url = ""

    def __init__(self, http):
        self.http = http
        self.last_error = ""
        self.served_stale = False      # 这次给的是不是"断网时顶上的旧缓存"

    # -- 公共 -------------------------------------------------------------
    def image_base(self):
        return self.http.cache.img_dir if self.http.cache else ""

    def _get(self, url, ttl=TTL_LIST, what="", timeout=None):
        try:
            obj = self.http.get_json(url, ttl=ttl, what=what, timeout=timeout)
            self.last_error = ""
            self.served_stale = bool(getattr(self.http, "served_stale", False))
            return obj
        except FeedNetError as e:
            self.last_error = str(e)
            raise
        except Exception as e:                      # 适配器自己的解析错也算"这个来源挂了"
            self.last_error = "解析失败（%s）" % type(e).__name__
            raise FeedNetError(self.last_error)

    def _cover(self, url):
        """封面图落地；失败就是没有封面（列表里少一张图，不影响别的）。"""
        if not url:
            return ""
        try:
            return self.http.fetch_image(url)
        except Exception:
            return ""

    def _localize_images(self, blocks, limit=12):
        """正文里的图也要落到本地：Qt 的 QTextDocument 读不了 http。

        **落不下来的图直接丢掉**（而不是留个 http 地址）：渲染层只会写出 <img src="...">，
        留 http 只会让正文里出现一排裂图标。宁可少几张图，不要一片坏图。"""
        out = []
        n = 0
        for b in (blocks or []):
            if b.kind != BLOCK_IMG:
                out.append(b)
                continue
            name = self._cover(b.url or b.text) if n < limit else ""
            if name:
                b.url = name
                n += 1
                out.append(b)
        return out


class IthomeSource(RealSource):
    """IT之家：免登录。列表走移动端接口（带封面和摘要），正文走 api 的 newscontent。"""

    key = "ithome"
    label = "IT之家"
    base_url = "https://www.ithome.com"
    CHANNELS = ("热榜", "最新")

    def channels(self):
        return list(self.CHANNELS)

    def _list_url(self, channel, page):
        if channel == "最新":
            return "https://m.ithome.com/api/news/newslistpageget?Tag=new&Page=%d" % page
        return "https://m.ithome.com/api/news/newslistpageget?Tag=hot&Page=%d" % page

    def _raw_items(self, channel, page):
        """拿一页原始条目。移动端接口挂了就退回 api/newslist/news（只有第一页）。"""
        try:
            data = self._get(self._list_url(channel, page), TTL_LIST, "IT之家列表",
                             timeout=LIST_TIMEOUT)
            items = data.get("Result") if isinstance(data, dict) else data
            if isinstance(items, list) and items:
                return items
        except FeedNetError:
            if page > 0:
                raise
        if page > 0:
            return []
        data = self._get("https://api.ithome.com/json/newslist/news", TTL_LIST, "IT之家列表",
                         timeout=LIST_TIMEOUT)
        toplist = (data or {}).get("toplist") or []
        newslist = (data or {}).get("newslist") or []
        if channel == "热榜":
            # api 里的 toplist 往往只有一条（今天的主推），单靠它这一页就废了：
            # 用 toplist 打头 + newslist 补齐，再去重。
            merged, seen = [], set()
            for it in list(toplist) + list(newslist):
                if not isinstance(it, dict):
                    continue
                nid = it.get("newsid")
                if nid in seen:
                    continue
                seen.add(nid)
                merged.append(it)
            return merged
        return newslist or toplist

    def _to_post(self, it, channel, want_cover=False):
        pid = str(it.get("newsid") or it.get("id") or "")
        ts = feed_mod.parse_time(it.get("postdate") or it.get("orderdate"))
        url = it.get("url") or ""
        if url and not url.lower().startswith("http"):
            url = self.base_url + (url if url.startswith("/") else "/" + url)
        return Post(
            source=self.key, source_label=self.label, channel=channel, pid=pid,
            title=feed_mod.strip_html(it.get("title") or ""),
            author=(it.get("newsauthor") or "").strip(),
            created=feed_mod.human_time(ts), ts=ts,
            score=int(it.get("hitcount") or 0),
            score_label="阅读",                 # hitcount 是阅读量，不是赞
            n_comments=int(it.get("commentcount") or 0),
            cover=self._cover(it.get("image")) if want_cover else "",
            excerpt=feed_mod.strip_html(it.get("description") or it.get("subtitle") or ""),
            url=url, tags=[],
        )

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.CHANNELS else self.CHANNELS[0]
        page = int(cursor or 0)
        items = self._raw_items(channel, page)
        posts = [self._to_post(it, channel, want_cover=(i < COVER_PER_PAGE))
                 for i, it in enumerate(items) if isinstance(it, dict)]
        posts = [p for p in posts if p.id and p.title]
        nxt = None
        if posts and page + 1 < MAX_PAGES:
            nxt = page + 1
        return posts, nxt

    def detail(self, post, cursor=None):
        data = self._get("https://api.ithome.com/json/newscontent/%s" % post.id,
                         TTL_DETAIL, "IT之家正文")
        if not isinstance(data, dict) or not data.get("success", True):
            raise FeedNetError("正文取不到")
        out = Post.from_dict(post.to_dict())
        out.body = self._localize_images(
            feed_mod.blocks_from_html(data.get("detail") or ""), limit=8)
        out.title = feed_mod.strip_html(data.get("title") or out.title)
        out.author = (data.get("newsauthor") or out.author or "").strip()
        ts = feed_mod.parse_time(data.get("postdate"))
        if ts:
            out.ts = ts
            out.created = feed_mod.human_time(ts)
        if data.get("hitcount"):
            out.score = int(data["hitcount"])
        if data.get("commentcount") is not None:
            out.n_comments = int(data["commentcount"])
        if data.get("image"):
            out.cover = self._cover(data["image"]) or out.cover
        tags = []
        for t in (data.get("newstags") or []):
            if isinstance(t, dict) and t.get("keyword"):
                tags.append(str(t["keyword"]))
        out.tags = tags or out.tags
        if data.get("url"):
            u = str(data["url"])
            out.url = u if u.lower().startswith("http") else self.base_url + u
        return out, None

    def comments(self, post, cursor=None):
        # IT之家没有可用的评论接口（2026-10-16 把 dyn/api/m/www 四条路都试过，全 404），
        # 正文页就只有正文。宁可明说没有，也不编评论。
        return [], None


class V2exSource(RealSource):
    """V2EX：官方公开 API（免登录）。注意本机网络到不了 v2ex.com，见 README。"""

    key = "v2ex"
    label = "V2EX"
    base_url = "https://www.v2ex.com"
    CHANNELS = ("最热", "最新")

    def channels(self):
        return list(self.CHANNELS)

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.CHANNELS else self.CHANNELS[0]
        api = "hot.json" if channel == "最热" else "latest.json"
        data = self._get("%s/api/topics/%s" % (self.base_url, api), TTL_LIST, "V2EX 列表",
                         timeout=LIST_TIMEOUT)
        items = data if isinstance(data, list) else (data or {}).get("result") or []
        posts = []
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("id") or "")
            if not pid:
                continue
            ts = feed_mod.parse_time(it.get("created") or it.get("last_modified"))
            member = it.get("member") or {}
            n = int(it.get("replies") or 0)
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=pid,
                title=feed_mod.strip_html(it.get("title") or ""),
                author=(member.get("username") or "") if isinstance(member, dict) else "",
                created=feed_mod.human_time(ts), ts=ts,
                # replies 是回复数、不是赞：别把它同时当 score，否则列表会写"12 赞 · 12 回复"
                score=0, n_comments=n, comment_label="回复",
                excerpt=feed_mod.strip_html(it.get("content") or it.get("content_rendered") or ""),
                url=it.get("url") or ("%s/t/%s" % (self.base_url, pid)),
                tags=list(it.get("tags") or []) if isinstance(it.get("tags"), list) else [],
            ))
        return posts, None            # 官方这两个接口本身就是一整页，没有分页

    def detail(self, post, cursor=None):
        data = self._get("%s/api/topics/show.json?id=%s" % (self.base_url, post.id),
                         TTL_DETAIL, "V2EX 正文")
        items = data if isinstance(data, list) else [data]
        it = items[0] if items and isinstance(items[0], dict) else {}
        out = Post.from_dict(post.to_dict())
        out.body = self._localize_images(feed_mod.blocks_from_html(
            it.get("content_rendered") or it.get("content") or ""), limit=6)
        member = it.get("member") or {}
        if isinstance(member, dict) and member.get("username"):
            out.author = member["username"]
        ts = feed_mod.parse_time(it.get("created"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        tags = it.get("tags")
        if isinstance(tags, list) and tags:
            out.tags = [str(t) for t in tags]
        return out, None

    def comments(self, post, cursor=None):
        data = self._get("%s/api/replies/show.json?topic_id=%s" % (self.base_url, post.id),
                         TTL_LIST, "V2EX 回复")
        items = data if isinstance(data, list) else []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            member = it.get("member") or {}
            ts = feed_mod.parse_time(it.get("created"))
            out.append(Comment(
                cid=str(it.get("id") or len(out)),
                author=(member.get("username") or "") if isinstance(member, dict) else "",
                created=feed_mod.human_time(ts), score=0,
                body=feed_mod.blocks_from_html(it.get("content_rendered") or it.get("content") or "",
                                               max_blocks=40),
            ))
        return out, None           # V2EX 回复是平铺的，没有层级


class DiscourseSource(RealSource):
    """标准 Discourse（linux.do 就是）。同一个类以后能直接接别的 Discourse 站。

    linux.do 前面挂着 Cloudflare 盾：必须贴浏览器 Cookie（含 cf_clearance、UA 要一致）。"""

    key = "linuxdo"
    label = "linux.do"
    base_url = "https://linux.do"
    CHANNELS = ("最新", "热门")

    def channels(self):
        return list(self.CHANNELS)

    def _endpoint(self, channel):
        return "/top.json" if channel == "热门" else "/latest.json"

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.CHANNELS else self.CHANNELS[0]
        page = int(cursor or 0)
        url = "%s%s?page=%d" % (self.base_url, self._endpoint(channel), page)
        data = self._get(url, TTL_LIST, "linux.do 列表", timeout=LIST_TIMEOUT)
        topics = ((data or {}).get("topic_list") or {}).get("topics") or []
        posts = []
        for i, t in enumerate(topics):
            if not isinstance(t, dict) or not t.get("id"):
                continue
            pid = str(t["id"])
            # 排序/显示都用"最后回复时间"：论坛的帖子热度看的是**还是不是活的**，
            # 拿建帖时间排会让一条今天还在聊的 2019 年老帖沉到最底下（混流里尤其明显）。
            ts = feed_mod.parse_time(t.get("last_posted_at") or t.get("created_at"))
            n = max(0, int(t.get("posts_count") or 1) - 1)
            slug = t.get("slug") or "t"
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=pid,
                title=feed_mod.strip_html(t.get("title") or t.get("fancy_title") or ""),
                author=(t.get("last_poster_username") or ""),
                created=feed_mod.human_time(ts), ts=ts,
                score=int(t.get("like_count") or 0), n_comments=n, comment_label="回复",
                cover=self._cover(t.get("image_url")) if i < COVER_PER_PAGE else "",
                excerpt=feed_mod.strip_html(t.get("excerpt") or ""),
                url="%s/t/%s/%s" % (self.base_url, slug, pid),
                tags=[str(x) for x in (t.get("tags") or [])],
            ))
        posts = [p for p in posts if p.title]
        # API 把置顶公告排在数组最前，而置顶公告的 last_posted_at 可能停在很多年前
        # （meta.discourse.org 那条 "New to Discourse? Start here!" 是 2013）——不重排的话，
        # 单看 linux.do 时最上面永远是一条陈旧公告。混流里它会自然沉下去，这里补上同一条规矩。
        posts.sort(key=lambda p: (p.ts or 0), reverse=True)
        nxt = page + 1 if posts and page + 1 < MAX_PAGES else None
        return posts, nxt

    def _topic(self, pid):
        return self._get("%s/t/%s.json" % (self.base_url, pid), TTL_DETAIL, "linux.do 正文")

    @staticmethod
    def _posts(data):
        return ((data or {}).get("post_stream") or {}).get("posts") or []

    def detail(self, post, cursor=None):
        data = self._topic(post.id)
        ps = self._posts(data)
        out = Post.from_dict(post.to_dict())
        first = ps[0] if ps and isinstance(ps[0], dict) else {}
        out.body = self._localize_images(
            feed_mod.blocks_from_html(first.get("cooked") or ""), limit=6)
        if first.get("username"):
            out.author = first["username"]
        ts = feed_mod.parse_time(first.get("created_at"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        score = first.get("reaction_users_count") or first.get("score")
        if score:
            out.score = int(score)
        if not out.title:
            out.title = feed_mod.strip_html(data.get("title") or "")
        return out, None

    def comments(self, post, cursor=None):
        """Discourse 的回复自带 reply_to_post_number，所以这里能还原出**真正的层级树**。"""
        data = self._topic(post.id)
        ps = [p for p in self._posts(data) if isinstance(p, dict)]
        if len(ps) <= 1:
            return [], None
        by_num = {}
        for p in ps[1:]:
            body = feed_mod.blocks_from_html(p.get("cooked") or "", max_blocks=40)
            score = p.get("reaction_users_count") or p.get("score") or 0
            by_num[p.get("post_number")] = Comment(
                cid=str(p.get("id") or p.get("post_number") or ""),
                author=(p.get("username") or p.get("name") or ""),
                created=feed_mod.human_time(feed_mod.parse_time(p.get("created_at"))),
                score=int(score or 0), body=body,
            )
        roots = []
        for p in ps[1:]:
            c = by_num.get(p.get("post_number"))
            if c is None:
                continue
            parent = by_num.get(p.get("reply_to_post_number"))
            if parent is not None and parent is not c:
                parent.replies.append(c)
            else:
                roots.append(c)
        # 兜底：万一数据里有环（谁回复了后面的人），把还没挂到树上的补到根上，
        # 保证"每条评论都能被看到"这件事不受数据形状影响
        placed = set()

        def mark(nodes):
            for n in nodes:
                placed.add(id(n))
                mark(n.replies)

        mark(roots)
        for c in by_num.values():
            if id(c) not in placed:
                roots.append(c)
        return roots, None


def fair_pages(merged, per_page=PAGE_MIX, cap=0):
    """把时间倒序的合并流切页，**每个来源每页最多 cap 条**，多出来的顺延到下一页。

    为什么需要它：IT之家一分钟一条，其它站几小时一条——纯按时间排的话"混流"第一页
    会被 IT之家整页占满，看着就不是混流。这里给每个来源一个每页席位，谁也不挤掉谁；
    被挤下去的条目只是往后挪一页，**一条都不会丢**（这一点有探针盯着）。
    """
    items = [p for p in (merged or []) if p is not None]
    if not items:
        return []
    if cap <= 0:
        return [items[i:i + per_page] for i in range(0, len(items), per_page)]
    pages = []
    pending = items
    guard = 0
    while pending and guard < 1000:
        guard += 1
        page, rest, counts = [], [], {}
        for p in pending:
            if len(page) >= per_page:
                rest.append(p)
                continue
            sid = getattr(p, "source", "")
            if counts.get(sid, 0) >= cap:
                rest.append(p)              # 这个来源这一页的席位满了，下一轮再来
                continue
            page.append(p)
            counts[sid] = counts.get(sid, 0) + 1
        if not page:                        # 理论上到不了（cap>=1 时每轮至少收一条）
            rest = pending
            page = pending[:per_page]
            rest = pending[per_page:]
        pages.append(page)
        pending = rest
    return pages


class ZhihuSource(RealSource):
    """知乎热榜。**只有列表这一层能免登录拿到**——这是实测出来的结论，不是没试。

    2026-10-16 在本机网络把几条路都走了一遍：
      - `api.zhihu.com/topstory/hot-list`（移动端接口）**不看 Cookie**：29 条热榜，
        每条带问题 id / 标题 / 摘要 / 回答数 / 关注数 / 热度文案 / 真原帖地址；
      - 详情全要签名或登录：`www.zhihu.com/api/v3/...hot-lists` 401「身份未经过验证」、
        `www.zhihu.com/hot` 与 `/billboard` 403（返回 `zh-zse-ck` 让你去算签名）、
        `api.zhihu.com/questions/<id>` 403「请求参数异常，请升级客户端」、
        答案列表 403 风控（试了加 `x-api-version` / App UA 也一样）；
      - 评论同理拿不到（没有 answer id 可谈）。

    所以这里**只做列表**：详情页摆出热榜给的摘要 + 一句实话，评论返回空。
    宁可少显示，也不编造正文——"拿不到就说拿不到"是这一轮反复用到的那条。
    """
    key = "zhihu"
    label = "知乎"
    base_url = "https://www.zhihu.com"
    needs_auth = False
    CHANNELS = ["热榜"]
    LIST_URL = "https://api.zhihu.com/topstory/hot-list?limit=50&reverse_order=0"

    def channels(self):
        return list(self.CHANNELS)

    def image_base(self):
        return self.http.cache.img_dir if self.http.cache is not None else ""

    @staticmethod
    def _hot(text):
        """把"1327 万热度"读成 13270000。读不出来就给 0（列表里干脆不显示这一项）。"""
        m = re.search(r"([\d.]+)\s*(亿|万)?", str(text or ""))
        if not m:
            return 0
        try:
            v = float(m.group(1))
        except ValueError:
            return 0
        unit = m.group(2)
        if unit == "亿":
            v *= 100000000
        elif unit == "万":
            v *= 10000
        return int(v)

    def list(self, channel=None, cursor=None):
        data = self._get(self.LIST_URL, TTL_LIST, "知乎热榜", timeout=LIST_TIMEOUT)
        items = (data or {}).get("data") or []
        posts = []
        for it in items:
            tgt = it.get("target") if isinstance(it, dict) else None
            if not isinstance(tgt, dict) or not tgt.get("id"):
                continue
            qid = str(tgt["id"])
            ts = feed_mod.parse_time(tgt.get("created"))
            posts.append(Post(
                source=self.key, source_label=self.label, channel="热榜", pid=qid,
                title=feed_mod.strip_html(tgt.get("title") or ""),
                author="",                      # 热榜不告诉你这是谁问的
                created=feed_mod.human_time(ts), ts=ts,
                score=self._hot(it.get("detail_text")), score_label="热度",
                n_comments=int(tgt.get("answer_count") or 0), comment_label="回答",
                excerpt=feed_mod.strip_html(tgt.get("excerpt") or ""),
                # target.url 是 api.zhihu.com 的接口地址；给人点的必须是网页地址，
                # 否则 O 打开的是 JSON（这条是探针里对着真实响应逼出来的）
                url="%s/question/%s" % (self.base_url, qid),
                tags=[],
            ))
        posts = [p for p in posts if p.id and p.title]
        return posts, None                  # 热榜就这一页，没有分页

    def detail(self, post, cursor=None):
        """知乎给不了正文：把列表现成的那段真摘要摆出来，再直说一句为什么只有这么点。"""
        out = Post.from_dict(post.to_dict())
        body = []
        if out.excerpt:
            body.append(Block(kind=BLOCK_P, text=out.excerpt))
        body.append(Block(kind=BLOCK_QUOTE,
                          text="知乎的正文和评论要登录才给（接口返回 401/403），"
                               "这里只显示热榜页给出的摘要。按 O 打开原帖看全文。"))
        out.body = body
        return out, None

    def comments(self, post, cursor=None):
        return [], None                     # 拿不到就是拿不到，不造评论


class WeiboSource(RealSource):
    """微博热搜。**只有"榜单"这一层免登录**——和知乎是同一类情况，但更薄。

    2026-10-16 实测：
      - `weibo.com/ajax/side/hotSearch` **不要 Cookie**：52 条词条，带 `num`（热度）、
        `label_name`（新 / 热 / 爆）、`word_scheme`（能直接拼搜索地址）；
      - 正文和评论都要登录：`m.weibo.cn/api/container/getIndex...` 返回 **HTTP 432**（空体），
        网页版接口要前端签名，`m.weibo.cn` 还要 `X-Requested-With` + 签名参数。
    所以这里是"热榜"形态：一条就是**一个词条**，详情页给的是热度 + 一句实话 + 搜索地址，
    不编造正文和评论。

    **默认关闭**（设计文档里微博属于"单独开关"的那一档）：`feed_sources` 里写
    `["ithome","v2ex","linuxdo","zhihu","weibo"]` 才会出现在来源列表里。
    """
    key = "weibo"
    label = "微博"
    base_url = "https://weibo.com"
    needs_auth = False
    CHANNELS = ("热搜",)
    HOT_URL = "https://weibo.com/ajax/side/hotSearch"

    def channels(self):
        return list(self.CHANNELS)

    def image_base(self):
        return self.http.cache.img_dir if self.http.cache is not None else ""

    def list(self, channel=None, cursor=None):
        data = self._get(self.HOT_URL, TTL_LIST, "微博热搜", timeout=LIST_TIMEOUT)
        items = ((data or {}).get("data") or {}).get("realtime") or []
        now = time.time()
        posts = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            word = feed_mod.strip_html(it.get("word") or "").strip()
            if not word:
                continue
            # 热榜没有"发布时间"——它本来就是此刻的榜单，所以按"现在"参与混流排序，
            # 不然 ts=0 会让它永远沉在混流最后（这是个诚实的选择，不是拍脑袋）。
            tags = []
            label = str(it.get("label_name") or "").strip()
            if label:
                tags.append(label)
            posts.append(Post(
                source=self.key, source_label=self.label, channel="热搜", pid=word,
                title=word, author="",
                created=feed_mod.human_time(now), ts=now,
                score=int(it.get("num") or 0), score_label="热度",
                n_comments=0,                     # 评论数拿不到，就不显示这一项
                excerpt="",                       # note 字段和 word 基本重复，不拿它凑摘要
                url=self._search_url(it.get("word_scheme") or word),
                tags=tags,
            ))
        return posts, None                        # 热搜就这一页

    @staticmethod
    def _search_url(word):
        """热搜词条给人点的地址。用 s.weibo.com 的搜索页：它认话题（#xx#）也认普通词。"""
        return "https://s.weibo.com/weibo?q=" + quote(str(word or ""), safe="")

    def detail(self, post, cursor=None):
        """微博给不了正文：把热度摆出来，直说为什么只有这么点，搜索地址留着给 O。"""
        out = Post.from_dict(post.to_dict())
        body = []
        if out.score:
            body.append(Block(kind=BLOCK_P, text="热度 %s（微博热搜榜）" % feed_mod._num(out.score)))
        body.append(Block(kind=BLOCK_QUOTE,
                          text="微博的正文和评论要登录才给（移动端接口返回 432、网页接口要前端签名），"
                               "这里只给热搜词条。按 O 去微博搜这个词看原帖。"))
        out.body = body
        return out, None

    def comments(self, post, cursor=None):
        return [], None                           # 拿不到就是拿不到，不造评论


class MixSource(SourceAdapter):
    """混流：把各来源的第一页合起来按时间倒序排，**默认就落在它上面**。

    详情/评论按 `post.source` 转给对应来源——所以在混流里点开一条帖子，正文和评论照样对。
    """

    key = "mix"
    label = "全部来源"
    needs_auth = False

    def __init__(self, sources):
        self.sources = [s for s in (sources or []) if s is not None]
        self.by_key = dict((s.key, s) for s in self.sources)
        self.last_error = ""
        self.errors = []
        self._skip_until = {}       # key -> 到什么时候为止先别碰它（刚失败过）
        self._lock = threading.Lock()
        self.stale = set()          # 这次给了"旧缓存顶上"的来源 key

    def channels(self):
        return ["全部"]

    def image_base(self):
        for s in self.sources:
            base = s.image_base()
            if base:
                return base
        return ""

    def _one(self, src, now, out, idx):
        """取一个来源的第一页。跑在自己的线程里，所以异常必须在这里收干净。"""
        try:
            self._skip_until.pop(src.key, None)
            channel = src.channels()[0] if src.channels() else None
            posts, _nxt = src.list(channel, 0)
            out[idx] = posts
            if getattr(src, "served_stale", False):
                # 这一家这次给的是"断网时顶上的旧缓存"：记下来，让页脚能说清
                with self._lock:
                    self.stale.add(src.key)
        except FeedNetError as e:
            with self._lock:
                self._skip_until[src.key] = time.time() + FAIL_COOLDOWN
            out[idx] = ("err", "%s：%s" % (src.label, e))
        except Exception as e:
            with self._lock:
                self._skip_until[src.key] = time.time() + FAIL_COOLDOWN
            out[idx] = ("err", "%s：%s" % (src.label, type(e).__name__))

    def list(self, channel=None, cursor=None):
        """并行问所有来源，再合页。

        为什么并行：V2EX 在本机网络根本连不上，串行的话整页要等它一个超时（10 秒）才出内容。
        并行之后最慢的那个决定等待时间，而且**它失败过就会被跳过 60 秒**，不会再挡路。"""
        page = int(cursor or 0)
        self.errors = []
        self.stale = set()
        now = time.time()
        todo = [s for s in self.sources if self._skip_until.get(s.key, 0) <= now]
        for s in self.sources:
            if s not in todo:
                self.errors.append("%s：刚才没连上，先跳过" % s.label)
        out = [None] * len(todo)
        if todo:
            threads = []
            for i, src in enumerate(todo):
                t = threading.Thread(target=self._one, args=(src, now, out, i), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=max(LIST_TIMEOUT * 2, 8))
        lists = []
        for item in out:
            if isinstance(item, tuple):
                self.errors.append(item[1])
            elif item:
                lists.append(item)
        self.last_error = "；".join(self.errors)
        merged = feed_mod.merge_posts(lists)
        # 每个来源的每页席位：来源越多席位越少，只有一个来源时不设限（那就不叫混流了）
        live = len(set(p.source for p in merged)) or 1
        cap = 0 if live <= 1 else max(2, PAGE_MIX // live)
        pages = fair_pages(merged, PAGE_MIX, cap)
        if page < 0 or page >= len(pages):
            return [], None
        nxt = page + 1 if page + 1 < len(pages) else None
        return pages[page], nxt

    def _route(self, post):
        src = self.by_key.get(getattr(post, "source", ""))
        if src is None:
            raise FeedNetError("这条帖子来自 %s，但该来源没启用" % (getattr(post, "source", "") or "未知来源"))
        return src

    def detail(self, post, cursor=None):
        return self._route(post).detail(post, cursor)

    def comments(self, post, cursor=None):
        return self._route(post).comments(post, cursor)


def build_real_sources(cache_root=None, config=None, log=None):
    """按配置建好来源。顺序就是界面里 `[`/`]` 的顺序：混流打头，然后 IT之家 / V2EX /
    linux.do / 知乎。"""
    config = config or {}
    cache = feed_net.Cache(cache_root) if cache_root else None
    auth = load_auth(os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_auth.json"))
    enabled = config.get("feed_sources", None)
    if enabled is None:
        # 配置里没写过 -> 四个真来源全开；写成空列表 = 用户把它们全关了（只剩离线示例）
        enabled = ["ithome", "v2ex", "linuxdo", "zhihu"]
    elif not isinstance(enabled, list):
        enabled = [str(enabled)]

    made = []
    for key, cls in (("ithome", IthomeSource), ("v2ex", V2exSource),
                     ("linuxdo", DiscourseSource), ("zhihu", ZhihuSource),
                     ("weibo", WeiboSource)):
        if key not in enabled:
            continue
        cred = auth.get(key) if isinstance(auth.get(key), dict) else {}
        http = Http(cache=cache,
                    cookies=_cookies_from(cred.get("cookie")),
                    user_agent=cred.get("user_agent") or feed_net.UA,
                    headers={"Referer": cls.base_url + "/"})
        src = cls(http)
        src.needs_auth = bool(getattr(cls, "needs_auth", False))
        made.append(src)

    if not made:
        return []
    return [MixSource(made)] + made


def _cookies_from(text):
    """把浏览器里复制来的 "a=1; b=2" 解析成 dict。"""
    out = {}
    for part in str(text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out
