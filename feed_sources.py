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

import concurrent.futures
import json
import os
import re
import threading
import time
from html import escape as html_escape
from urllib.parse import quote, unquote

import feed as feed_mod
import hupu_boards
from feed import Block, Comment, Post, SourceAdapter, BLOCK_IMG, BLOCK_P, BLOCK_QUOTE
import feed_net
import xhs_sign
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

    def __init__(self, http, auth=None):
        self.http = http
        # 浏览器里复制来的 {"cookie": "...", "user_agent": "..."}（来自 feed_auth.json）。
        # 来源自己决定怎么用；没配就是空字典，"未配置"是一条正常状态，不是错误。
        self.auth = dict(auth or {})
        self.last_error = ""
        self.served_stale = False      # 这次给的是不是"断网时顶上的旧缓存"

    # -- 凭据 -------------------------------------------------------------
    @property
    def has_auth(self):
        return bool(str(self.auth.get("cookie") or "").strip())

    def auth_state(self):
        """给界面看的四个字：**配了没配**比"这个站要不要登录"更有用。

        知乎的列表免登录、正文要登录，所以"要不要"是一个来源内部就分层的概念；
        而"你贴没贴 Cookie"永远是当前状态，页脚/详情里要说的就是它。"""
        if self.has_auth:
            return "Cookie 已配置"
        return "Cookie 未配置（这个来源要登录）" if self.needs_auth else "免登录"

    def auth_note(self):
        """配了没配、配了管不管用——详情页里那句实话要用这个拼。"""
        if not self.has_auth:
            return "（这个站的正文/评论要登录，可以在应用目录的 feed_auth.json 里贴一次 Cookie）"
        return "（已经贴了 Cookie，但接口这次没给——见下面的原文）"

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
        if getattr(self, "images_off", False):
            # 低调模式下列表只画 [图]：封面一张都不用下（一页 6 张封面 ≈ 一秒）。
            # 返一个"不是文件名"的占位（没有点号），渲染层据此写 [图]，不会当成图片路径。
            return "lowkey"
        try:
            return self.http.fetch_image(url)
        except Exception:
            return ""

    def _localize_images(self, blocks, limit=12):
        """正文里的图也要落到本地：Qt 的 QTextDocument 读不了 http。

        **落不下来的图直接丢掉**（而不是留个 http 地址）：渲染层只会写出 <img src="...">，
        留 http 只会让正文里出现一排裂图标。宁可少几张图，不要一片坏图。

        2026-10-19：改成**并发**下。以前是一张一张排队下，一篇 12 张图的帖子光图就要
        十几秒（每张失败还得等超时），人看到的就是"点进帖子转半天"。现在最多 8 张同时
        下，顺序仍按正文原来的次序摆；单张的超时也压短了（图是锦上添花，不值得为它等）。
        """
        blocks = list(blocks or [])
        if getattr(self, "images_off", False):
            # 低调模式：正文只画 [图]，那就一张都别下（这条笔记能省掉一半以上的等待）。
            # 图块保留、url 清空：位置和"这里有图"的信息都还在，切回正常模式只要重进一次。
            return [Block(b.kind, b.text, "", b.lang) if b.kind == BLOCK_IMG else b
                    for b in blocks]
        slots = [(i, b) for i, b in enumerate(blocks) if b.kind == BLOCK_IMG][:limit]
        got = {}
        if slots:
            def one(item):
                idx, b = item
                raw = str(b.url or b.text or "")
                if raw and not raw.lower().startswith(("http", "//")):
                    return idx, raw          # 已经是本地文件名了，别再当地址去下
                return idx, self._cover(b.url or b.text)

            workers = min(8, len(slots))
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    for idx, name in ex.map(one, slots):
                        if name:
                            got[idx] = name
            except Exception:
                # 并发本身出问题就退回串行：慢一点也要把能落的图落下
                for idx, b in slots:
                    name = self._cover(b.url or b.text)
                    if name:
                        got[idx] = name
        out = []
        for i, b in enumerate(blocks):
            if b.kind != BLOCK_IMG:
                out.append(b)
                continue
            name = got.get(i)
            if name:
                b.url = name
                out.append(b)
        return out


class IthomeSource(RealSource):
    """IT之家：免登录。列表走移动端接口（带封面和摘要），正文走 api 的 newscontent。"""

    key = "ithome"
    label = "IT之家"
    base_url = "https://www.ithome.com"
    CHANNELS = ("热榜", "最新")
    COMMENT_API = "https://cmt.ithome.com/api/webcomment/getnewscomment"
    # 正文页里评论容器上的那个短 id，16 位十六进制
    COMMENT_SN_RE = re.compile(r'data-id="([0-9a-f]{16})"')
    COMMENT_LIMIT = 30

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

    def _comment_sn(self, post):
        """评论接口要的那个短 id：从正文页里捞 `data-id="<16 位十六进制>"`。

        每篇 IT之家文章都要多拉一次网页（约 30 KB）才拿得到它，所以走正文的长缓存；
        捞不到就当成"这篇没有评论"，绝不去猜一个 id 乱问。
        """
        if not post.url:
            return ""
        try:
            raw = self.http.get_bytes(post.url, ttl=TTL_DETAIL, max_bytes=2000000, timeout=12)
        except Exception:
            return ""
        m = self.COMMENT_SN_RE.search(raw.decode("utf-8", "replace"))
        return m.group(1) if m else ""

    def _to_comment(self, c, i=0):
        """一条 IT之家评论：`elements[]` 是正文片段，`children[]` 是回复，`support` 是赞。"""
        ui = c.get("userInfo") or {}
        text = "".join(str(e.get("content") or "") for e in (c.get("elements") or [])
                       if isinstance(e, dict)).strip()
        # 正文为空（只有图/表情）就干脆不摆正文：以前这里塞一句"（这条评论只有图片或表情）"，
        # 那是给开发者看的注释，不是内容。真人说过前端不要这种话。
        blocks = [Block(kind=BLOCK_P, text=text)] if text else []
        # 回复作为**真子节点**往下缩一层（reddit 那样）。以前是折成一行
        # `「李四：同意」` 塞进父评论正文——看着像引用、其实是回复，层级全丢了，
        # 真人的原话就是"评论和子评论没处理好，应该像 reddit 那样"。
        replies = [self._to_comment(k, j) for j, k in enumerate(
            [x for x in (c.get("children") or []) if isinstance(x, dict)][:8])]
        ts = feed_mod.parse_time(c.get("postTime"))
        return Comment(
            cid=str(c.get("id") or ("c%d" % i)),
            author=(ui.get("userNick") or "").strip() or "某人",
            created=feed_mod.human_time(ts) if ts else "",
            score=int(c.get("support") or 0),
            body=blocks,
            replies=replies,
        )

    def comments(self, post, cursor=None):
        """IT之家的评论：`cmt.ithome.com/api/webcomment/getnewscomment?sn=<短 id>`。
        2026-10-19 才摸对：这个接口的 `sn` **不是新闻号**，是正文页里那个 16 位十六进制的
        `data-id`（`sn` 拿新闻号去问，它一律回 `{"success":false,"message":"新闻不存在"}`）。
        以前把它当成"IT之家没有评论接口"，其实是问错了 id。评论分 `topComments` /
        `hotComments` / `comments` 三组，每条有 `support`（赞）/ `elements[]`（正文片段）/
        `children[]`（回复）/ `expandCount`（一共几条回复），回复并进正文更好读。
        """
        sn = self._comment_sn(post)
        if not sn:
            return [], None
        data = self._get("%s?sn=%s&cid=0&isInit=true&appver=900" % (self.COMMENT_API, sn),
                         TTL_LIST, "IT之家评论", timeout=LIST_TIMEOUT)
        if not isinstance(data, dict) or data.get("success") is False:
            return [], None
        content = data.get("content") or {}
        if not isinstance(content, dict):
            return [], None
        rows, seen = [], set()
        for key in ("topComments", "hotComments", "comments"):
            for c in (content.get(key) or []):
                if isinstance(c, dict) and c.get("id") not in seen:
                    seen.add(c.get("id"))
                    rows.append(c)
        return [self._to_comment(c, i) for i, c in enumerate(rows[:self.COMMENT_LIMIT])], None


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
                # Discourse 的 tags 是对象（{id,name,slug}），`str(x)` 会把整个字典
                # 摆到界面上（真人看到的是一串 "{'id': 26, ...}"）。这里只取名字。
                tags=[str(x.get("name") or x.get("slug") or "") if isinstance(x, dict)
                      else str(x) for x in (t.get("tags") or [])],
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
    """知乎：列表免登录，正文/评论**贴了 Cookie 就能拿到**（这一点实测过了）。

    2026-10-16 在本机网络把几条路都走了一遍（当时的结论是"只有列表能免登录"）：
      - `api.zhihu.com/topstory/hot-list`（移动端接口）**不看 Cookie**：29 条热榜，
        每条带问题 id / 标题 / 摘要 / 回答数 / 关注数 / 热度文案 / 真原帖地址；
      - 免登录时详情全要签名或登录：`www.zhihu.com/api/v3/...hot-lists` 401「身份未经过验证」、
        `www.zhihu.com/hot` 与 `/billboard` 403（返回 `zh-zse-ck` 让你去算签名）、
        `api.zhihu.com/questions/<id>` 403「请求参数异常，请升级客户端」、
        答案列表 403 风控（试了加 `x-api-version` / App UA 也一样）。

    **贴上浏览器 Cookie 之后结论变了**（2026-10-16 复测）：答案接口
    `/api/v4/questions/<qid>/answers?include=...&limit=1` 直接 200、正文是 `data[0].content` 的 HTML；
    评论接口 `/api/v4/answers/<aid>/comments` 也 200（15 条）。所以之前担心的
    `x-zse-96` 前端签名在这一层**并不需要**——"光有 Cookie 不够"那句猜测已经被实测推翻，
    代码和文档都不再这么说。

    所以现在的分工：列表免登录；**配了 Cookie 才去试回答**，试到了就把回答摆进"评论"那一栏，
    试不到就把原因写在正文里（不编回答、不编评论）。

    **回答就是这一页的"评论"**（真人原话：「知乎显示的应该是评论而不是问题的内容」）。
    知乎的一个问题底下没有"评论"这种东西，只有回答——所以详情页的评论那一栏就是回答列表，
    正文那一栏只留问题自己的信息。以前是把热榜页给的问题摘要当正文摆着，那正是"显示的是问题
    的内容、不是评论"。
    """
    key = "zhihu"
    label = "知乎"
    base_url = "https://www.zhihu.com"
    needs_auth = False
    CHANNELS = ["热榜", "推荐", "关注"]
    LIST_URL = "https://api.zhihu.com/topstory/hot-list?limit=50&reverse_order=0"
    # 推荐：2026-10-19 用真 Cookie 实测 200，`data[i].target` 是**回答**（type=answer，
    # 自带 excerpt/content/question/author），`paging.next` 给下一页的完整 URL。
    RECOMMEND_URL = "https://api.zhihu.com/topstory/recommend?action=down&ad_interval=-1&limit=10"
    # 关注：同一套形状（`data[i].target` 是回答或文章）。没人可关注时它就是空列表，
    # 不报错——空着如实显示"这一栏还没内容"，不要假装网络出问题。
    FOLLOW_URL = "https://api.zhihu.com/moments?limit=10&offset=0"
    # 一次问这么多条回答：它们就是详情页的"评论"那一栏。**每条都摆全文**，
    # 所以从 10 收到 6——再多一页就是几万字，翻起来像在读一本书。
    ANSWERS_LIMIT = 6
    # 只有前几条回答去拉"它自己的评论"：一条回答一次请求，全拉一遍要去十几次。
    # 一页 6 条回答，**每条都要带自己的评论**：以前只给前 3 条补，
    # 后 3 条永远是空的——真人 2026-10-19 报的就是这个"有些回答的评论好像不会加载"。
    ANSWERS_WITH_COMMENTS = 6
    CHILD_COMMENTS_LIMIT = 12    # 每条评论再带几条子评论（先摆 3 条，其余"继续加载"）
    ANSWER_COMMENTS_LIMIT = 8
    # 一条回答最多摆多少块（一块 = 一个段落或一张图）。
    # **帖子正文那一份不设小上限**：真人要的就是"能读完一条长回答"，按 40 块截断等于又省略了
    # （实测一条 9000 字的回答有 100+ 块）。300 块是防疯的兜底，正常回答根本到不了。
    ANSWER_BLOCKS_BODY = 300
    # 摆成"评论"的回答每条 60 块：一页里可能有 6 条，再多就翻不动了。
    ANSWER_BLOCKS_COMMENT = 60
    # 正文短于这么多字就怀疑是被截断了（单条回答接口对长答案是"前 N 段 + 展开"）。
    TRUNC_HINT = 400
    # 列表里的摘要长度（`_excerpt_of`）。详情页的回答走全文，不用这把尺子。
    EXCERPT_CHARS = 220
    # 失败只记这么久。以前失败也缓存一辈子，于是第一次撞上风控之后，这个问题到进程结束都打不开。
    RETRY_AFTER = 90

    def __init__(self, http, auth=None):
        RealSource.__init__(self, http, auth)
        self._ans = {}          # 问题 id -> (回答列表, 失败原因, 取到的时刻)：详情和评论共用一次请求

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
        ch = channel if channel in self.CHANNELS else "热榜"
        if ch == "热榜":
            return self._list_hot(ch)
        return self._list_feed(ch, cursor)

    def _post_from_question(self, tgt, channel, score_text=""):
        """热榜/推荐里"问题"那一种：标题、热度、回答数、网页地址。"""
        qid = str(tgt.get("id") or "")
        if not qid:
            return None
        ts = feed_mod.parse_time(tgt.get("created"))
        return Post(
            source=self.key, source_label=self.label, channel=channel, pid=qid,
            title=feed_mod.strip_html(tgt.get("title") or ""),
            author="",                          # 热榜不告诉你这是谁问的
            created=feed_mod.human_time(ts), ts=ts,
            score=self._hot(score_text), score_label="热度",
            n_comments=int(tgt.get("answer_count") or 0), comment_label="回答",
            excerpt=self._clip(feed_mod.strip_html(tgt.get("excerpt") or "")),
            # target.url 是 api.zhihu.com 的接口地址；给人点的必须是网页地址，
            # 否则 O 打开的是 JSON（这条是探针里对着真实响应逼出来的）
            url="%s/question/%s" % (self.base_url, qid),
            tags=[],
        )

    def _post_from_answer(self, tgt, channel):
        """推荐/关注里"回答"那一种：题目取它所在的问题，正文只留摘要（缩略）。

        id 前面加 `a` 区分"这是一条回答"而不是"一个问题"——详情/评论两条路要靠它分流，
        免得拿回答 id 去问问题接口。
        """
        aid = str(tgt.get("id") or "")
        q = tgt.get("question") if isinstance(tgt.get("question"), dict) else {}
        qid = str(q.get("id") or "")
        if not aid:
            return None
        ts = feed_mod.parse_time(tgt.get("created_time"))
        url = ("%s/question/%s/answer/%s" % (self.base_url, qid, aid)) if qid \
            else ("%s/answer/%s" % (self.base_url, aid))
        return Post(
            source=self.key, source_label=self.label, channel=channel, pid="a" + aid,
            title=feed_mod.strip_html(q.get("title") or "") or "（这条回答没有问题标题）",
            author=_zhihu_name(tgt.get("author")) or "匿名",
            created=feed_mod.human_time(ts), ts=ts,
            score=int(tgt.get("voteup_count") or 0), score_label="赞",
            n_comments=int(tgt.get("comment_count") or 0), comment_label="评论",
            excerpt=self._excerpt_of(tgt),
            url=url, tags=[],
        )

    def _clip(self, text):
        """一段文字压到 `EXCERPT_CHARS` 以内（压成一行，超了加省略号）。缩略就用这一把尺子。"""
        s = " ".join(str(text or "").split())
        if len(s) > self.EXCERPT_CHARS:
            s = s[:self.EXCERPT_CHARS].rstrip() + "…"
        return s

    def _excerpt_of(self, item):
        """一条回答的摘要：优先用接口给的 `excerpt`，没有再拿正文截断。一律压成一行。"""
        ex = feed_mod.strip_html(item.get("excerpt") or "")
        if len(ex) < 12:
            ex = feed_mod.strip_html(item.get("content") or "")
        return self._clip(ex)

    def _list_hot(self, channel):
        data = self._get(self.LIST_URL, TTL_LIST, "知乎热榜", timeout=LIST_TIMEOUT)
        posts = []
        for it in ((data or {}).get("data") or []):
            tgt = it.get("target") if isinstance(it, dict) else None
            if not isinstance(tgt, dict):
                continue
            p = self._post_from_question(tgt, channel, score_text=it.get("detail_text"))
            if p is not None:
                posts.append(p)
        return [p for p in posts if p.id and p.title], None    # 热榜就这一页，没有分页

    def _list_feed(self, channel, cursor):
        """推荐 / 关注：一次一页，分页游标就是接口自己给的 `paging.next` 完整地址。"""
        first = self.RECOMMEND_URL if channel == "推荐" else self.FOLLOW_URL
        url = cursor or first
        data = self._get(url, TTL_LIST, "知乎" + channel, timeout=LIST_TIMEOUT)
        data = data or {}
        posts = []
        for it in (data.get("data") or []):
            tgt = it.get("target") if isinstance(it, dict) else None
            if not isinstance(tgt, dict):
                continue
            kind = str(tgt.get("type") or "")
            p = None
            if kind == "answer":
                p = self._post_from_answer(tgt, channel)
            elif kind == "question":
                p = self._post_from_question(tgt, channel)
            if p is not None:
                posts.append(p)
        posts = [p for p in posts if p.id and p.title]
        nxt = ((data.get("paging") or {}).get("next") or "").strip() or None
        if nxt == url:            # 有的接口把当前页原样回给你，别让它无限翻
            nxt = None
        return posts, nxt

    def _is_answer_post(self, post):
        return str(post.id).startswith("a") or "/answer/" in (post.url or "")

    def _question_id(self, post):
        """回答帖 → 它属于哪个问题（从自己生成的网页地址里取，取不到就空）。"""
        m = re.search(r"/question/(\d+)", post.url or "")
        if m:
            return m.group(1)
        pid = str(post.id or "")
        return pid if pid.isdigit() else ""

    def detail(self, post, cursor=None):
        """正文栏只放**问题自己**的信息；回答属于"评论"那一栏（见 comments）。

        真人原话：「知乎显示的应该是评论而不是问题的内容」。知乎的一个问题底下只有回答、
        没有"评论"，所以这一页的评论栏就是回答列表。正文栏不再把问题摘要当正文摆着，
        只留一句"回答在下面"或者**没拿到的原因**（401 未登录 / 403 风控是两回事，分开说）。

        **配了 Cookie 就真去试一次**——不然"要登录"永远只是猜测，人也不知道自己贴的 Cookie
        到底管不管用；试的结果照实写。热榜页给的问题摘要不会丢：它不是回答，所以标成"问题摘要"
        放在最后一行，方便看这条问题到底在问什么。
        """
        out = Post.from_dict(post.to_dict())
        body = []
        if self._is_answer_post(post):
            # 推荐/关注里点开的是一条回答：正文走**全文**（`/api/v4/answers/<id>`），
            # 不是列表那 220 字摘要——真人要的是"能看完整条回答"。
            # 拿不到全文时退回摘要（**绝不空着**：上一版这里给过一页空白）。
            item, _why = self._answer_full(post.id) if self.has_auth else ({}, "")
            blocks = self._localize_images(
                feed_mod.blocks_from_html(str((item or {}).get("content") or ""),
                                          max_blocks=self.ANSWER_BLOCKS_BODY), limit=4)
            if not blocks:
                txt = (out.excerpt or "").strip() or (post.excerpt or "").strip()
                blocks = [Block(kind=BLOCK_P, text=txt)] if txt else []
            if item.get("question") and item["question"].get("title"):
                out.title = feed_mod.strip_html(item["question"]["title"]) or out.title
            if item.get("voteup_count"):
                out.score, out.score_label = int(item["voteup_count"]), "赞"
            if item.get("comment_count"):
                out.n_comments = int(item["comment_count"])
            out.body = blocks
            return out, None
        answers, why = self._answers(str(post.id)) if self.has_auth else ([], "")
        if answers:
            pass             # 回答在"评论"那一栏里，正文不再写一句说明
        elif not self.has_auth:
            # 只留一句事实。以前这里是一大段"怎么配 Cookie / 按 O 看全文"，
            # 那是说明书，不是帖子内容（真人：前端不要注释性语言）。
            body.append(Block(kind=BLOCK_QUOTE, text="回答需要登录"))
        else:
            body.append(Block(kind=BLOCK_QUOTE, text="回答没取到（%s）" % (why or "没给原因")))
        if out.excerpt and not _zhihu_junk_excerpt(out.excerpt):
            body.append(Block(kind=BLOCK_P, text=out.excerpt))
        if not body and (out.excerpt or "").strip():
            # 兜底：一句话都没有的时候宁可把摘要摆上，也不给一页空白。
            body.append(Block(kind=BLOCK_P, text=out.excerpt.strip()))
        out.body = body
        return out, None

    def _answer_full(self, aid):
        """一条回答的**全文**：`/api/v4/answers/<数字 id>`（2026-10-19 实测免签名，HTTP 200）。

        上一版详情里只有列表给的 220 字摘要，真人一句"知乎的长答案直接被省略了"就点在这：
        接口明明给全文，却只摆了个摘要。返回 (dict, 失败原因)。
        """
        aid = str(aid).lstrip("a")
        key = "a:" + aid
        hit = None if feed_net.is_fresh() else self._ans.get(key)
        if hit is not None:
            item, why, when = hit
            if item or (time.time() - when) < self.RETRY_AFTER:
                return item, why
        url = ("%s/api/v4/answers/%s?include=content,excerpt,voteup_count,comment_count,"
               "created_time,author.name,question.title" % (self.base_url, aid))
        item, why = {}, ""
        try:
            data = self.http.get_json(url, ttl=TTL_DETAIL, what="知乎回答正文",
                                      headers={"x-requested-with": "fetch"}, timeout=LIST_TIMEOUT)
        except FeedNetError as e:
            why = str(e)
        except Exception as e:
            why = type(e).__name__
        else:
            item = data if isinstance(data, dict) else {}
            if not (item.get("content") or item.get("excerpt")):
                why = "接口回了 200 但没内容"
            elif item.get("content_need_truncated") or len(str(item.get("content") or "")) < self.TRUNC_HINT:
                # 单条接口对长回答只给前几百字（`content_need_truncated: true`），
                # 但"问题的回答列表"里那份是**完整的**。拿 question.id 去列表里捞同一条。
                qid = str(((item.get("question") or {}).get("id")) or "")
                if qid:
                    rows, _w = self._answers(qid)
                    for r in rows:
                        if str(r.get("id") or "") == aid:
                            item = dict(item)
                            item["content"] = r.get("content") or item.get("content")
                            item["content_need_truncated"] = False
                            break
        self._ans[key] = (item, _zhihu_why(why) if why else "", time.time())
        return self._ans[key][0], self._ans[key][1]

    def _child_comments(self, cid, limit=None):
        """一条评论底下的**子评论**（第二层）。

        知乎只有两层：评论 → 子评论；再往里的回复是被摊平到同一层、用 `@某人` 开头的，
        所以这里最多再取一层就够了（取太多层反而每次都要多好几个请求）。
        接口实测：`/api/v4/comments/<cid>/child_comments?limit=N&offset=0`（2026-10-19）。
        """
        cid = str(cid)
        key = "cc:" + cid
        hit = None if feed_net.is_fresh() else self._ans.get(key)
        if hit is not None:
            items, _why, when = hit
            if items or (time.time() - when) < self.RETRY_AFTER:
                return list(items)
        limit = int(limit or self.CHILD_COMMENTS_LIMIT)
        url = ("%s/api/v4/comments/%s/child_comments?limit=%d&offset=0"
               % (self.base_url, cid, limit))
        out = []
        try:
            data = self.http.get_json(url, ttl=TTL_DETAIL, what="知乎子评论",
                                      headers={"x-requested-with": "fetch"},
                                      timeout=LIST_TIMEOUT)
        except Exception:
            data = {}
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        for i, it in enumerate([r for r in (rows or []) if isinstance(r, dict)]):
            body = feed_mod.blocks_from_html(str(it.get("content") or ""), max_blocks=4)
            if not body:
                continue
            out.append(Comment(
                cid=str(it.get("id") or ("zcc%d" % i)),
                author=_zhihu_name(it.get("author")) or "匿名",
                created=feed_mod.human_time(feed_mod.parse_time(it.get("created_time"))),
                score=int(it.get("vote_count") or it.get("like_count") or 0),
                body=body,
            ))
        self._ans[key] = (out, "", time.time())
        return out

    def _answer_comments(self, aid, limit=None, children=True, offset=0):
        """一条回答底下的评论（知乎的"评论"就是这一层，挂在回答下面当子节点）。

        `children=True` 时再给前几条有回复的评论各取一层子评论（真三层树：
        回答 → 评论 → 子评论）。问题页那种"一屏摆 6 条回答"的场景传 `children=False`：
        那里要的是广度，为每条回答再补子评论只会把一页拖成十几秒。
        `author` 是 `{role, member:{name}}` 那种嵌一层的形状，`_zhihu_name` 认得出。
        """
        aid = str(aid).lstrip("a")
        key = "c:%s:%d" % (aid, int(offset or 0))
        hit = None if feed_net.is_fresh() else self._ans.get(key)
        if hit is not None:
            items, _why, when = hit
            if items or (time.time() - when) < self.RETRY_AFTER:
                return list(items)
        limit = int(limit or self.ANSWER_COMMENTS_LIMIT)
        url = ("%s/api/v4/answers/%s/comments?limit=%d&offset=%d&order=normal"
               "&include=data[*].content,like_count,created_time,author.name,child_comment_count"
               % (self.base_url, aid, limit, int(offset or 0)))
        out = []
        try:
            data = self.http.get_json(url, ttl=TTL_DETAIL, what="知乎回答评论",
                                      headers={"x-requested-with": "fetch"},
                                      timeout=LIST_TIMEOUT)
        except Exception:
            data = {}
        rows = [r for r in ((data or {}).get("data") or []) if isinstance(r, dict)]
        out = []
        built = {}
        for i, it in enumerate(rows):
            body = feed_mod.blocks_from_html(str(it.get("content") or ""), max_blocks=6)
            if not body:
                continue
            c = Comment(
                cid=str(it.get("id") or ("zc%d" % i)),
                author=_zhihu_name(it.get("author")) or "匿名",
                created=feed_mod.human_time(feed_mod.parse_time(it.get("created_time"))),
                score=int(it.get("vote_count") or it.get("like_count") or 0),
                body=body,
            )
            out.append(c)
            built[i] = c
        if children and built:
            # 这份接口**不给** `child_comment_count`（字段清单里根本没有），所以没法"只问有回复的"：
            # 老老实实给前三条各问一次子评论，问空就是没有（三个请求并发，约 200 毫秒）。
            need = []
            for i in sorted(built):
                cid = str(rows[i].get("id") or "") if i < len(rows) else ""
                if cid:
                    need.append((i, cid))
            need = need[:3]              # 一页只补前三条：再多就是拿等待换密度
            if need:
                got = {}

                def one(idx, cid):
                    try:
                        got[idx] = self._child_comments(cid)
                    except Exception:
                        got[idx] = []

                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(3, len(need))) as ex:
                    for _f in [ex.submit(one, i, c) for i, c in need]:
                        try:
                            _f.result(timeout=15)
                        except Exception:
                            pass
                for idx, reps in got.items():
                    if idx in built and reps:
                        built[idx].replies = reps
        self._ans[key] = (out, "", time.time())
        return out

    def comments_page(self, post, cursor=None):
        """评论翻页：**只有"点开的是某条回答"这一层有下一页**（offset 往后翻）。

        问题页那层摆的是这个问题的回答，一页 `ANSWERS_LIMIT` 条就是全部，没有下一页。
        缓存键里带上了 offset，所以翻过的页再翻回来是 0 请求。
        """
        if not self.has_auth:
            return [], None, None
        if not self._is_answer_post(post):
            items, why = self.comments(post)
            return items, why, None
        off = max(0, int(cursor or 0))
        cmts = self._answer_comments(post.id, offset=off)
        # 下一页游标 = 已经读到的条数。**不能拿"这一页够不够 limit"判断**：
        # 实测接口一次只回 7 条（limit 写 8），按 limit 判就永远以为没有下一页。
        # 知道总数（post.n_comments）就按总数停；不知道就让下一页空着回来（空页即到头）。
        total = int(getattr(post, "n_comments", 0) or 0)
        got = off + len(cmts)
        nxt = got if (cmts and (total <= 0 or got < total)) else None
        return cmts, None, nxt

    def _answers(self, qid):
        """问一次这个问题的回答列表。返回 (list[dict], 失败原因)。

        URL 的形状是**拿真 Cookie 试出来的**（2026-10-16：HTTP 200，`data[i].content` 是正文 HTML）。
        同一个问题只成功问一次（详情和评论共用这一份）；**失败只记 RETRY_AFTER 秒**——
        以前失败也缓存一辈子，第一次撞上风控之后这个问题就永远打不开了。
        """
        qid = str(qid)
        hit = None if feed_net.is_fresh() else self._ans.get(qid)
        if hit is not None:
            items, why, when = hit
            if items or (time.time() - when) < self.RETRY_AFTER:
                return items, why
        url = ("%s/api/v4/questions/%s/answers"
               "?include=data[*].content,excerpt,voteup_count,comment_count,created_time,author.name"
               "&limit=%d&offset=0&order=default" % (self.base_url, qid, self.ANSWERS_LIMIT))
        items, why = [], ""
        try:
            data = self.http.get_json(url, ttl=TTL_DETAIL, what="知乎回答",
                                      headers={"x-requested-with": "fetch"}, timeout=LIST_TIMEOUT)
        except FeedNetError as e:
            why = str(e)
        except Exception as e:
            why = type(e).__name__
        else:
            raw = (data or {}).get("data") if isinstance(data, dict) else None
            if isinstance(raw, list):
                items = [it for it in raw if isinstance(it, dict)]
                if not items:
                    why = "接口回了 200 但一条回答都没有（可能改版了）"
            elif isinstance(raw, dict):
                items = [raw]
            else:
                why = "接口回的形状不认识"
        self._ans[qid] = (items, _zhihu_why(why) if why else "", time.time())
        return self._ans[qid][0], self._ans[qid][1]

    def comments(self, post, cursor=None):
        """这一栏的"评论"就是**回答列表**——真人要的正是它们，能一条条读、能折叠。

        **只摆摘要**（`EXCERPT_CHARS` 以内）：以前把整条回答的 HTML 都塞进来，一条几千字，
        十条规定下来一页几万字，人翻两屏就不想读了（真人原话：「知乎的热榜详情内容应该缩略
        而不是全文显示」）。要全文按 O 开原帖。

        取不到就如实为空（原因写在正文那一栏，不在这里重复）。注意回答作者是 `author.name`
        那一层（评论才是嵌一层的 `author.member.name`，两种形状 `_zhihu_name` 都认）。
        """
        if not self.has_auth:
            return [], None
        if self._is_answer_post(post):
            # 点开的是一条回答：评论栏就是**这条回答底下**的评论（问题→回答→评论三层里的第三层）。
            return self._answer_comments(post.id), None
        qid = self._question_id(post)
        if not qid:
            return [], None
        answers, _why = self._answers(qid)
        # 前几条回答"各自的评论"是独立请求。串着一个一个来，一页要多等一两秒
        # （实测 4 条串行 2.4 秒）；并发取回来，顺序和条数一条不少。
        need = {}
        for i, it in enumerate(answers):
            aid0 = str(it.get("id") or "")
            if (aid0 and i < self.ANSWERS_WITH_COMMENTS
                    and int(it.get("comment_count") or 0) > 0):
                need[i] = aid0
        got = {}
        if need:
            def _one(idx, aid0):
                try:
                    got[idx] = self._answer_comments(aid0, children=False)
                except Exception:
                    got[idx] = []

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(4, len(need))) as ex:
                for _f in [ex.submit(_one, i, a) for i, a in need.items()]:
                    try:
                        _f.result(timeout=20)
                    except Exception:
                        pass
        out = []
        for i, it in enumerate(answers):
            # 回答摆**全文**：它是"评论"这一层的正文，缩成摘要就变成"长答案被省略"了。
            blocks = self._localize_images(
                feed_mod.blocks_from_html(str(it.get("content") or ""),
                                          max_blocks=self.ANSWER_BLOCKS_COMMENT), limit=3)
            if not blocks:
                txt = self._excerpt_of(it)
                blocks = [Block(kind=BLOCK_P, text=txt)] if txt else []
            aid = str(it.get("id") or "")
            reps = got.get(i) or []
            out.append(Comment(
                cid=aid or ("a%d" % i),
                author=_zhihu_name(it.get("author")) or "匿名",
                created=feed_mod.human_time(feed_mod.parse_time(it.get("created_time"))),
                score=int(it.get("voteup_count") or 0),
                body=blocks,
                replies=reps,
            ))
        return out, None


def _zhihu_junk_excerpt(text):
    """热榜页的"摘要"经常是 `[2 张图片]` 这种，不是内容，不该当问题摘要摆出来。"""
    s = str(text or "").strip()
    if len(s) < 6:
        return True
    return bool(re.fullmatch(r"[\[【(（]?\s*\d+\s*张图片\s*[\]】)）]?", s))


def _zhihu_why(why):
    """把接口错误说成人话。401 和 403 是两回事，别混成一句"Cookie 过期了"。

    实测（2026-10-16）：风控时返回的是 403 + 「您当前请求存在异常，暂时限制本次访问」+
    一串十六进制 trace id——**几分钟后就恢复**（同一段代码几分钟前还在正常取 20 条评论）。
    所以那句"常见原因是 Cookie 过期了"是误导：Cookie 好好的，只是被限流了。
    """
    s = str(why or "")
    low = s.lower()
    if "401" in s:
        return "知乎说没登录（401）。feed_auth.json 里 \"zhihu\" 的 Cookie 没了或者不完整。"
    if "403" in s or "风控" in s or "暂时限制" in s:
        return ("知乎在风控限流（403「暂时限制本次访问」）。这**不是** Cookie 过期——"
                "等几分钟再按 R 重试通常就好了。")
    if "412" in s:
        return "知乎把这次请求当成脚本了（412）。等一会儿再试，或者换一次 Cookie。"
    if "超时" in s or "timeout" in low:
        return "请求超时（网络或者对面慢了）：" + s
    return s


def _zhihu_name(author):
    """知乎的"作者"有两种形状：`{"name": ...}`（回答）和 `{"role":..., "member":{"name":...}}`（评论）。

    认不出来就给空字符串——宁可显示"匿名"，也不显示 `{'role': 'normal'}` 这种给人看的乱码。"""
    if not isinstance(author, dict):
        return ""
    if author.get("name"):
        return str(author["name"])
    member = author.get("member")
    if isinstance(member, dict) and member.get("name"):
        return str(member["name"])
    return ""


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
        # 微博只给热搜词条：正文接口要登录 + 前端签名，这一层没接（详情见 README）。
        # 以前这里塞了一大段解释，属于"注释性语言进前端"，删掉；拿不到就是没有正文。
        out.body = body
        return out, None

    def comments(self, post, cursor=None):
        return [], None                           # 拿不到就是拿不到，不造评论


def _nga_recode(text):
    """如果 `text` 是"UTF-8 被当成 GBK 解开"的乱码，返回还原后的文本；否则返回 None。

    判据很干脆：**按 gb18030 编回去还能得到合法 UTF-8** —— 那就说明原文本来就是 UTF-8，
    刚才那一步解错了。反过来，正常的 GBK 中文编回 gb18030 之后不是合法 UTF-8，不会被误判。
    （用 gb18030 而不是 gbk 反解：真机上这种乱码多半是"宽编码"解出来的，gbk 编不回去；
    这种乱码还常常丢掉末尾一两个字节，所以补几次"切掉尾巴再试"。）
    """
    if not text:
        return None
    body = text.replace("\ufffd", "")
    if not body or all(ord(ch) < 128 for ch in body):
        return None
    for enc in ("gb18030", "gbk"):
        try:
            back = body.encode(enc)
        except Exception:
            continue
        for cut in (0, 1, 2):
            chunk = back[:len(back) - cut] if cut else back
            try:
                fixed = chunk.decode("utf-8")
            except Exception:
                continue
            if any("\u4e00" <= ch <= "\u9fff" for ch in fixed):
                return fixed
        return None                        # 编回去了但解不出中文 → 不是这种乱码
    return None


def _nga_mojibake(text):
    """这段文本是不是乱码（"UTF-8 被当 GBK 解"那一种，也就是人眼看到的样子）。"""
    return _nga_recode(text) is not None


def _nga_unmojibake(text):
    """把一段乱码还原回正常中文（还原不了就原样返回）。"""
    fixed = _nga_recode(text)
    return text if fixed is None else fixed


def _nga_fix_tree(node):
    """递归把整棵 JSON 里的乱码字符串修回来。

    为什么要按**字符串**修、而不是整份响应只挑一个编码：NGA 这两条接口的编码实测并不统一
    （2026-10-19：`thread.php` 是 GBK，`read.php` 里带用户输入的那些字段会是 UTF-8），
    一份响应里两种编码混着来的时候，选哪个都会有一半是乱码。"""
    if isinstance(node, str):
        return _nga_unmojibake(node)
    if isinstance(node, list):
        return [_nga_fix_tree(x) for x in node]
    if isinstance(node, dict):
        return {k: _nga_fix_tree(v) for k, v in node.items()}
    return node


def _nga_parse(raw):
    """NGA 的 `__output=8` JSON：**必须 `strict=False`**。

    它返回的字符串里有裸换行/制表符（JSON 标准不允许），`json.loads` 默认会
    `Invalid control character` 直接报错——这不是"接口坏了"，是它就是这么发的。

    编码这块**不能只信它自称的**：响应里写着 `"encode":"gbk"`，但同一个站的另一条接口
    （`read.php`）实际发的是 UTF-8。所以先按"自称的编码 + 另一种"各解一遍，谁不像乱码用谁，
    解析完再按字符串兜一遍（见 `_nga_fix_tree`）——真人反馈的"NGA 全是乱码"就是死信自称值造成的。
    """
    if isinstance(raw, (bytes, bytearray)):
        head = bytes(raw[:600]).lower()
        declared_gbk = b'"encode"' in head and b"gbk" in head
        order = ("gbk", "utf-8") if declared_gbk else ("utf-8", "gbk")
        text = None
        for enc in order:
            try:
                cand = raw.decode(enc)
            except Exception:
                continue
            if "\ufffd" in cand or _nga_mojibake(cand):
                continue                   # 这一解出来是乱码，换另一种
            text = cand
            break
        if text is None:
            text = raw.decode("gbk", "replace")
    else:
        text = str(raw)
    try:
        data = json.loads(text, strict=False)
    except Exception as e:
        raise FeedNetError("NGA 回的 JSON 读不出来（%s）" % type(e).__name__)
    return _nga_fix_tree(data)


def _nga_rows(data, key):
    """把 `data.__T` / `data.__R` 那种"数字键字典"收成按楼层排好的列表。

    `thread.php` 的 `__T` 是**帖子表**（值是 dict）；`read.php` 的 `__T` 是**主题元信息**
    （平字典，值是字符串）——同一个键名两种含义，所以认不出 dict 就返回空列表，而不是硬解。"""
    node = (data or {}).get("data") if isinstance(data, dict) else None
    block = (node or {}).get(key) if isinstance(node, dict) else None
    if isinstance(block, list):
        items = list(block)
    elif isinstance(block, dict):
        try:
            items = [block[k] for k in sorted(block.keys(), key=lambda x: int(x))]
        except Exception:
            items = list(block.values())
    else:
        items = []
    return [it for it in items if isinstance(it, dict)]


def _nga_op(data):
    """`read.php` 的楼主帖 = `__R` 里 `lou == 0` 的那条（没有就退回第一条）。"""
    rows = _nga_rows(data, "__R")
    for it in rows:
        if int(it.get("lou") or 0) == 0:
            return it
    return rows[0] if rows else {}


def _nga_author(data, it):
    """作者名：先用楼里自带的名字，再查 `__U` 用户表。

    实测（2026-10-16）NGA 的接口**对其他用户只给 `UID:12345678` 这种占位串**，不给真昵称
    （只有楼主帖的 `__T.author` 是真名）。所以这里认不出真名就返回空字符串：
    页面上宁可少显示作者，也不显示 `UID:67020159` 这种给人看的占位串。"""
    for k in ("author", "username"):
        v = it.get(k)
        if isinstance(v, str) and v.strip() and not v.strip().startswith("UID:"):
            return v.strip()
    users = ((data or {}).get("data") or {}).get("__U") if isinstance(data, dict) else None
    node = (users or {}).get(str(it.get("authorid"))) if isinstance(users, dict) else None
    name = str((node or {}).get("username") or "").strip() if isinstance(node, dict) else ""
    return "" if name.startswith("UID:") else name


# NGA 的常用板块表（栏目名 → fid）。
#
# NGA 是"一个站里几百个版面"的形态，所以这一家的栏目就是**板块**。官方的板块树在
# `app_api.php?__lib=home&__act=category&_v=2`，**不需要 Cookie**（2026-10-16 实测：
# 200、7 个分类 → 分组 → 661 个板块，每条带 fid/name/info）。600 多条放进菜单没法看，
# 所以界面分两层：这张短表当默认菜单，`NgaSource.boards()` 拉全量给"更多板块…"用。
#
# 下面这十条是从官方分类树里**逐条核过**的（名字和 fid 都对得上）：
#   网事杂谈 -7 / 晴风村 -7955747 / 寂寞的车 -343809 / 生命之杯 -81981 / 漩涡书院 524 /
#   国际新闻 843 / 音乐影视 -576177 / 程序员职业交流 -202020 /
#   消费电子 IT新闻 436 / 娱乐吃瓜区 -39223361
# 记一笔**没放进来**的：TouchFish 扩展的 `touchfish.ngaTab` 默认枚举里有
# 「股票大时代=706」「Cosplay=472」「消费电子=436」「娱乐吃瓜=-39223361」——
# 后两个在官方树里的名字分别是「消费电子 IT新闻」「娱乐吃瓜区」（fid 一致，已收进来）；
# 前两个在官方树里**找不到同名条目**（要么是隐藏版面、要么是它抄的），所以不进默认表：
# 想看得在「更多板块…」里填 fid。别人给的默认值不该被我们当成事实。
NGA_BOARDS = (
    ("网事杂谈", "-7"),
    ("晴风村", "-7955747"),
    ("寂寞的车", "-343809"),
    ("生命之杯", "-81981"),
    ("漩涡书院", "524"),
    ("国际新闻", "843"),
    ("音乐影视", "-576177"),
    ("程序员职业交流", "-202020"),
    ("消费电子 IT新闻", "436"),
    ("娱乐吃瓜区", "-39223361"),
)


def _nga_boards(data):
    """NGA 的板块分类树 → `[{"label", "fid", "group"}]`（按官方顺序，重复的去掉）。

    真实形状（2026-10-16 实测 `app_api.php?__lib=home&__act=category&_v=2`）：
        {"code": 0, "result": [{"name": "网事杂谈", "groups": [
            {"name": "网事杂谈", "forums": [{"fid": -7, "name": "网事杂谈", "info": null}, ...]},
            ...]}, ...]}
    **fid 有时候是整数、有时候是字符串**（同一份响应里两种都有，比如 -7 和 "-7955747"），
    所以统一成字符串再比；同名同 fid 的重复条目（实测「手游评分版=863」这类同名不同 fid 的
    反而要留着，只有完全相同的才算重复）只留第一条。"""
    out, seen = [], set()
    for cat in (data or {}).get("result") or []:
        if not isinstance(cat, dict):
            continue
        for g in cat.get("groups") or []:
            if not isinstance(g, dict):
                continue
            for f in g.get("forums") or []:
                if not isinstance(f, dict):
                    continue
                label = feed_mod.strip_html(f.get("name") or "")
                fid = f.get("fid")
                fid = "" if fid is None else str(fid).strip()
                if not label or not fid or (label, fid) in seen:
                    continue
                seen.add((label, fid))
                out.append({"label": label, "fid": fid, "group": str(g.get("name") or "")})
    return out


class NgaSource(RealSource):
    """NGA（艾泽拉斯国家地理）。**免登录完全进不去**，必须贴浏览器 Cookie。

    2026-10-16 实测（本机网络、浏览器 Cookie）：
      - 免登录：`thread.php?fid=-7&__output=8` 返 **403 + GBK 的 `{"error":{"0":"1:未登录"}}`**，
        首页也直接写着游客不能访问；
      - **贴上 Cookie 后列表 200**（49 条）：每条带 `tid / subject / author / postdate / lastpost /
        replies`；
      - **正文**走 `read.php?tid=<tid>&__output=8`：`data.__T` 是**主题元信息**（`subject` / `author` /
        `lastpost`），`data.__R` 是**按楼层**的字典（`lou=0` 是楼主帖，其余是回复），
        每层有 `content`（HTML 夹 BBCode）/ `authorid` / `postdatetimestamp` / `score`；
      - **老帖子会 403**，返回体写着 `5:帖子发布或回复时间超过限制`——**这不是登录问题**，
        是 NGA 自己的规矩（列表第一条恰好就是这种老帖，当时误以为是 Cookie 不对）。
        这种情况按原话报出来，不谎称"要登录"；
      - 回复**没有真正的层级**（引用写在正文里），所以按平铺给。

    解析全部按真响应写（`__T` 的两种含义、`strict=False`、GBK、`__U` 占位名都处理了）。

    **板块（fid）**：NGA 是"一个站里有几百个版面"的形态，所以这一家的栏目就是板块。
    默认只给 `NGA_BOARDS` 那张短表（工位上会看的十来个），`boards()` 能拉全量
    （官方分类树 661 个，**不要 Cookie**），界面里用可搜索的列表让人挑任意一条或直接填 fid。
    """

    key = "nga"
    label = "NGA"
    base_url = "https://bbs.nga.cn"
    needs_auth = True                  # 这个是真的要登录：列表都进不去
    CHANNELS = tuple(label for label, _fid in NGA_BOARDS)
    FID = dict(NGA_BOARDS)
    MAX_PAGE_BYTES = 4000000
    BOARDS_URL = "https://bbs.nga.cn/app_api.php?__lib=home&__act=category&_v=2"

    def __init__(self, http, auth=None):
        RealSource.__init__(self, http, auth)
        self._boards = []              # 全量板块表（`boards()` 拉到之后放这儿）

    def channels(self):
        """栏目 = 板块。拉过全量表就用全量（界面里能滚能搜），否则就是那张短表。"""
        if self._boards:
            return [b["label"] for b in self._boards]
        return list(self.CHANNELS)

    def board_fid(self, channel):
        """栏目名（或直接给的 fid）→ NGA 的 fid。认不出来就退回默认板块。"""
        s = str(channel or "").strip()
        for label, fid in NGA_BOARDS:
            if s == label or s == fid:
                return fid
        for b in self._boards:
            if s == b["label"] or s == b["fid"]:
                return b["fid"]
        return NGA_BOARDS[0][1]

    def board_label(self, fid):
        """fid → 人看的名字（认不出来就把 fid 本身当名字，别显示空白）。"""
        s = str(fid or "").strip()
        for label, f in NGA_BOARDS:
            if s == f:
                return label
        for b in self._boards:
            if s == b["fid"]:
                return b["label"]
        return s

    def boards(self, force=False):
        """拉全量板块表：`[{"label", "fid", "group"}]`（按官方分类树的顺序）。

        **不需要 Cookie**（2026-10-16 实测：200，661 个板块）。拉不到就退回短表——
        换板块是件小事，拉不到全量不该让整页报错，但也不能假装拿到了。"""
        if self._boards and not force:
            return list(self._boards)
        try:
            raw = self.http.get_bytes(self.BOARDS_URL, ttl=TTL_DETAIL,
                                      max_bytes=self.MAX_PAGE_BYTES, timeout=LIST_TIMEOUT)
            data = _nga_parse(raw)
        except Exception:
            return [{"label": label, "fid": fid, "group": ""} for label, fid in NGA_BOARDS]
        got = _nga_boards(data)
        if got:
            self._boards = got
        return list(self._boards) if self._boards else [
            {"label": label, "fid": fid, "group": ""} for label, fid in NGA_BOARDS]

    def image_base(self):
        return self.http.cache.img_dir if self.http.cache is not None else ""

    def _json(self, url, ttl, what, timeout=None):
        raw = self.http.get_bytes(url, ttl=ttl, max_bytes=self.MAX_PAGE_BYTES, timeout=timeout)
        return _nga_parse(raw)

    @staticmethod
    def _attach_base(data):
        """图片的相对路径前缀（响应里自带，实测 `img.nga.cn/attachments`）。"""
        node = ((data or {}).get("data") or {}).get("__GLOBAL") if isinstance(data, dict) else None
        base = (node or {}).get("_ATTACH_BASE_VIEW") if isinstance(node, dict) else ""
        return str(base or "img.nga.cn/attachments").strip("/")

    def list(self, channel=None, cursor=None):
        fid = self.board_fid(channel)
        channel = self.board_label(fid)
        url = "%s/thread.php?fid=%s&__output=8" % (self.base_url, fid)
        data = self._json(url, TTL_LIST, "NGA 列表", timeout=LIST_TIMEOUT)
        posts = []
        for it in _nga_rows(data, "__T"):
            tid = it.get("tid")
            title = feed_mod.strip_html(it.get("subject") or "")
            if not tid or not title:
                continue
            # 列表上的时间是"最后回复"——板块页本来就是"哪里还在动"，这比发布时间更有用
            ts = feed_mod.parse_time(it.get("lastpost") or it.get("postdate"))
            author = feed_mod.strip_html(it.get("author") or "")
            if ts <= 0 or not author:
                # 真夹具抓到的：`thread.php` 的 `__T` 里会混进一条**它自己的错误提示**
                # （`subject` 是"帖子发布或回复时间超过限制"，没有作者也没有时间）。
                # 当帖子收下的话，列表第一条就会变成一句报错——所以这里按"有没有作者和时间"筛掉。
                continue
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=str(tid),
                title=title,
                author=author,
                created=feed_mod.human_time(ts), ts=ts,
                score=0,                       # 列表里没有"赞"这种数，只有回复数
                n_comments=max(0, int(it.get("replies") or 0)), comment_label="回复",
                excerpt="",
                url="%s/read.php?tid=%s" % (self.base_url, tid),
                tags=[str(it.get("type"))] if str(it.get("type") or "") not in ("", "0") else [],
            ))
        posts.sort(key=lambda p: (p.ts or 0), reverse=True)
        return posts, None

    def detail(self, post, cursor=None):
        url = "%s/read.php?tid=%s&__output=8" % (self.base_url, post.id)
        try:
            data = self._json(url, TTL_DETAIL, "NGA 正文")
        except FeedNetError as e:
            if "时间超过限制" in str(e):
                raise FeedNetError("NGA 不让看这条老帖（它自己说的：帖子发布或回复时间超过限制）")
            raise
        out = Post.from_dict(post.to_dict())
        meta = ((data or {}).get("data") or {}).get("__T")
        meta = meta if isinstance(meta, dict) else {}
        first = _nga_op(data)
        if meta.get("subject"):
            out.title = feed_mod.strip_html(meta["subject"])
        name = feed_mod.strip_html(str(meta.get("author") or "")) or _nga_author(data, first)
        if name:
            out.author = name
        ts = feed_mod.parse_time(first.get("postdatetimestamp") or first.get("postdate"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        out.body = self._localize_images(
            _nga_blocks(first.get("content") or "", limit=40, attach_base=self._attach_base(data)),
            limit=8)
        if not out.body:
            out.body = [Block(kind=BLOCK_QUOTE, text="正文没取到")]
        return out, None

    def comments(self, post, cursor=None):
        return self._comments(post, 1)[0], None

    def comments_page(self, post, cursor=None):
        """NGA 的回复按楼层分页（`read.php?tid=..&page=N`，一页 20 楼）。

        以前只取第 1 页，读到底就没了；现在滚到底自动接下一页。"""
        page = max(1, int(cursor or 1))
        items, nxt = self._comments(post, page)
        return items, None, nxt

    def _comments(self, post, page=1):
        url = "%s/read.php?tid=%s&page=%d&__output=8" % (self.base_url, post.id, int(page))
        try:
            data = self._json(url, TTL_LIST, "NGA 回复")
        except FeedNetError:
            return [], None                    # 拿不到就如实为空（老帖 403 是常态）
        out = []
        base = self._attach_base(data)
        rows = _nga_rows(data, "__R")
        # NGA 对别人只给 `UID:…` 占位；引用表头里有真名，被引用过的人能反查出来
        names = _nga_name_from_quotes(rows)
        for it in rows:
            if int(it.get("lou") or 0) == 0:
                continue                       # 楼主帖已经在上面的正文里了
            body = self._localize_images(
                _nga_blocks(it.get("content") or "", attach_base=base), limit=6)
            if not body:
                continue
            ts = feed_mod.parse_time(it.get("postdatetimestamp") or it.get("postdate"))
            out.append(Comment(
                cid=str(it.get("pid") or it.get("lou") or len(out)),
                author=_nga_author(data, it) or names.get(str(it.get("authorid")), ""),
                created=feed_mod.human_time(ts),
                score=0,                       # score 这个字段的含义没验证过，不往"赞"上摆
                body=body,
                reply_to=_nga_reply_to(it.get("content") or ""),
            ))
        # 还有没有下一页：拿"本页最大楼层"和"这个帖一共几楼"比。
        # 不能用"本页条数>=20"来判断——最后一页只有三两楼，那样会永远以为还有下一页。
        total = int(((data.get("data") or {}).get("__ROWS")) or 0)
        max_lou = max([int(it.get("lou") or 0) for it in rows] or [0])
        nxt = (page + 1) if (total and max_lou + 1 < total) else None
        return out, nxt


_NGA_UID_RE = re.compile(r"\[uid=[^\]]*\]\s*([^\[\]]{1,32}?)\s*\[/uid\]", re.I)


def _nga_quote_inner(raw):
    """正文开头引用块的内部文本（没有引用块返回空串）。"""
    m = re.match(r"\s*\[quote\](.*?)(?:\[/quote\]|$)", str(raw or ""), re.S | re.I)
    return m.group(1) if m else ""


def _nga_quote_head_len(inner):
    """引用块里"表头"占多长：到第一个 `[/b]` 为止。

    实测见过两种（2026-10-19）：

        [quote][pid=885058074,47745238,1]Reply[/pid] [b]Post by [uid=2202186]某某[/uid]
        (2026-10-19 11:47):[/b]<br/><br/>被引用的那段[/quote]<br/>这条回复自己的话
        [quote]Reply to [pid=..]Reply[/pid] [b]Post by [uid=..]asas123456789[/uid]
        (2026-10-19 17:35):[/b]…

    **按 `[b]…[/b]` 切，别按字面词切**：`Reply` / `Reply to` / `发表于` 这些写法都见过，
    逐种写正则会漏一种就在正文里留一行机器话。没有 `[b]` 的老帖退回到"时间戳结束处"。
    """
    text = str(inner or "")
    cut = re.search(r"\[/b\]", text)
    if cut:
        return cut.end()
    cut = re.search(r"\d{1,2}:\d{2}\s*[)）]?\s*:?|<br\s*/?>|\n", text)
    return cut.end() if cut else 0


def _nga_reply_to(raw):
    """这条回复回的是谁：引用块表头里 `[uid=…]名字[/uid]` 的那个名字。"""
    inner = _nga_quote_inner(raw)
    m = _NGA_UID_RE.search(inner[:_nga_quote_head_len(inner)])
    return m.group(1).strip() if m else ""


def _nga_name_from_quotes(rows):
    """从各楼引用块的表头里收集 `uid -> 昵称`。

    NGA 的接口对**别人**只给 `UID:12345678` 这种占位串（未登录或权限不够时），但它自己的
    引用表头里写的是真名。所以"被引用过的人"能靠这个反查出来——比一片"匿名"强，
    也不用瞎编：查不到的照旧不显示名字。
    """
    out = {}
    for it in rows or []:
        head = _nga_quote_inner(it.get("content") or "")
        head = head[:_nga_quote_head_len(head)]
        for m in re.finditer(r"\[uid=(\d+)\]\s*([^\[\]]{1,32}?)\s*\[/uid\]", head, re.I):
            uid, name = m.group(1), m.group(2).strip()
            if name and not name.startswith("UID:"):
                out[uid] = name
    return out


def _nga_blocks(html, limit=40, attach_base="img.nga.cn/attachments"):
    """NGA 的正文是 HTML 里夹 BBCode（`[quote]` / `[b]` / `[img]` / `[pid=...]`）。

    `[img]` 里的地址常常是**相对路径**（形如 `./mon_202610/16/xxx.jpg`），
    直接扔给解析层就会变成一条没用的文字。真实前缀由响应里的
    `__GLOBAL._ATTACH_BASE_VIEW` 给（实测是 `img.nga.cn/attachments`），
    所以这里先补成完整地址，交给 `_localize_images` 落盘。

    只处理"不处理就会看着别扭"的那几种标记，不做完整 BBCode 实现——
    认不出来的标记原样留着也比猜错强。"""
    text = str(html or "")

    def _img(m):
        src = (m.group(1) or "").strip()
        if src.startswith("./"):
            src = src[2:]
        if src and not src.lower().startswith(("http", "//", "data:")):
            src = "https://%s/%s" % (str(attach_base or "").strip("/"), src.lstrip("/"))
        elif src.startswith("//"):
            src = "https:" + src
        return '<img src="%s"/>' % src

    text = re.sub(r"\[img\](.*?)\[/img\]", _img, text, flags=re.I | re.S)
    def _quote(m):
        # 引用块的表头（`[pid=..]Reply[/pid] [b]Post by [uid=..]某人 (时间):[/b]`）不再是正文的一部分：
        # 收件人已经在表头写成"回复 某人"，留在这里就是一行"Reply Post by ..."的机器话。
        # 剩下被引用的那段包成 <blockquote>，排成引用样式（一眼看出这段是别人说的）。
        inner = m.group(1)
        return "<blockquote>%s</blockquote>" % inner[_nga_quote_head_len(inner):]

    text = re.sub(r"\[quote\](.*?)\[/quote\]", _quote, text, flags=re.I | re.S)
    text = re.sub(r"\[/?[a-z=*\u4e00-\u9fff][^\]]{0,40}\]", "", text)   # [b] [/b] [pid=..] [uid=..]
    return feed_mod.blocks_from_html(text, max_blocks=limit)


def _xhs_state(html):
    """从小红书页面里抠出 `window.__INITIAL_STATE__` 并当 JSON 解。

    两处真坑（都是实测踩到的，不是假想）：
    1. **不能用非贪婪正则抠**——对象里第一个 `}<` 出现在字符串里（`"...} <..."`），
       正则会提前截断，实测少 1.5 KB 而且必然解不开。所以按括号配对扫，还得跳过字符串和转义；
    2. 它是 **JS 字面量**，里面有 `undefined` 和 `new Map([])` / `new Set([])` 这种 JSON 不认的写法，
       得先替换掉再当 JSON 解。
    """
    at = str(html or "").find("window.__INITIAL_STATE__")
    if at < 0:
        raise FeedNetError("小红书的页面里没有状态块（改版了，或者返回的是空壳页）")
    at = html.find("{", at)
    depth, i, n, in_str, quote, esc = 0, at, len(html), False, "", False
    text = None
    while 0 <= at < n and i < n:
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
                text = html[at:i + 1]
                break
        i += 1
    if not text:
        raise FeedNetError("小红书的页面状态读不出来（括号没配对，多半是改版了）")
    text = re.sub(r"\bundefined\b", "null", text)
    text = re.sub(r"\bnew Map\(\[\]\)", "{}", text)
    text = re.sub(r"\bnew Set\(\[\]\)", "[]", text)
    text = re.sub(r"\bnew Map\(\[.*?\]\)", "{}", text, flags=re.S)
    text = re.sub(r"\bnew Set\(\[.*?\]\)", "[]", text, flags=re.S)
    try:
        return json.loads(text)
    except Exception as e:
        raise FeedNetError("小红书的页面状态解不开（%s）" % type(e).__name__)


def _xhs_text(desc):
    """正文里的 `#话题[话题]#` 标记：去掉标记只留话题名（读得顺，也不会把 `[话题]` 当正文）。"""
    t = re.sub(r"#([^#\[\]]{1,40})\[话题\]#", r"\1", str(desc or ""))
    t = html_escape(t)
    return re.sub(r"\n{2,}", "<br/><br/>", t.replace("\n", "<br/>"))


def _xhs_cover(cover):
    """封面图地址：列表里的 `cover.infoList[0].url`（`url` 字段本身常常是空的）。"""
    if not isinstance(cover, dict):
        return ""
    for k in ("urlDefault", "url"):
        if cover.get(k):
            return str(cover[k])
    for it in (cover.get("infoList") or []):
        if isinstance(it, dict) and it.get("url"):
            return str(it["url"])
    return ""


def _xhs_comments(obj):
    """把 `/api/sns/web/v2/comment/page` 的返回翻成 Comment。

    形状（它自己的前端就是这么读的）：`data.comments[]`，每条 `id` / `content` /
    `create_time`（毫秒）/ `like_count` / `user_info.nickname` / `sub_comments[]`
    （子评论同样有 `content` / `user_info`），`data.cursor` + `data.has_more` 管翻页。
    """
    data = ((obj or {}).get("data") or {}) if isinstance(obj, dict) else {}
    rows = data.get("comments") or []
    out = []
    for i, it in enumerate(rows):
        if not isinstance(it, dict):
            continue
        blocks = feed_mod.blocks_from_html(it.get("content") or "", max_blocks=8)
        # 同上：空正文就是空正文，不补说明句。子评论作为**真子节点**往下缩一层。
        subs = []
        for j, sub in enumerate([x for x in (it.get("sub_comments") or [])
                                 if isinstance(x, dict)][:8]):
            sblocks = feed_mod.blocks_from_html(sub.get("content") or "", max_blocks=6)
            who = ((sub.get("user_info") or {}).get("nickname") or "").strip()
            if not sblocks and not who:
                continue
            subs.append(Comment(
                cid=str(sub.get("id") or ("s%s-%d" % (it.get("id"), j))),
                author=who or "某人",
                created=feed_mod.human_time(int(sub.get("create_time") or 0) / 1000.0),
                score=int(sub.get("like_count") or 0),
                body=sblocks,
            ))
        out.append(Comment(
            cid=str(it.get("id") or ("c%d" % i)),
            author=((it.get("user_info") or {}).get("nickname") or "").strip() or "某人",
            created=feed_mod.human_time(int(it.get("create_time") or 0) / 1000.0),
            score=int(it.get("like_count") or 0),
            body=blocks,
            replies=subs,
        ))
    return out


class XhsSource(RealSource):
    """小红书。**免登录只有空壳**，要贴浏览器 Cookie——这个站把内容服务端渲染在页面里。

    2026-10-16 实测（本机网络、浏览器 Cookie）：
      - 免登录：`/explore` 是一张没有内容的 SPA 壳（36 KB 左右）；
      - **带上 Cookie：`/explore` 228 KB，`window.__INITIAL_STATE__` 里 `feed.feeds` 30+ 条**，
        每条有 `id` / `xsecToken` / `noteCard`（`displayTitle` / `type` / `user.nickName` /
        `interactInfo.likedCount` / `cover.infoList[0].url`）；
      - 详情：`/explore/<id>?xsec_token=<token>&xsec_source=pc_feed` 200，
        `note.noteDetailMap[<id>].note` 里有 `desc`（正文）/ `imageList` / `tagList` /
        `interactInfo`；`time` 是**毫秒**时间戳（不除 1000 会显示成 5 万年后）；
      - 视频笔记跳过（`noteCard.type == "video"`）：这个窗口不放视频，收下来也只是个壳。

    **评论怎么拿到的**：`/api/sns/web/v2/comment/page` 不接受裸 Cookie，每个请求都要
    `x-s` / `x-t` / `x-s-common`。那套算法在 `xhs_sign.py` 里（照 VS Code 摸鱼插件
    TouchFish 的纯 JS 实现搬的，逐字节对得上）。

    2026-10-19 记一笔踩过的坑：一开始 `x-s` 发的是 `mns0301_…` 那一段，服务端一律
    **406 `{"code":-1,"success":false}`**。真相是那一段只是**内层**——真正的 `x-s`
    还要把它包一层：`"XYS_" + b64(JSON{x0:版本,x1:应用,x5:md5})`，`x3` 才是那段
    `mns0301_`。包上之后同一条请求直接 **200 且 `comments` 有内容**。当年还误判成
    "插件自己也被 406"，其实是自己把内层当成了外层——`probe_sources.py` §17 现在钉着
    这层信封的向量，别再退回内层。
    """

    key = "xhs"
    label = "小红书"
    base_url = "https://www.xiaohongshu.com"
    needs_auth = True                  # 免登录拿到的是一张空壳页
    CHANNELS = ("推荐",)
    MAX_PAGE_BYTES = 3000000
    COMMENT_API = "/api/sns/web/v2/comment/page"
    COMMENT_URL = "https://edith.xiaohongshu.com" + COMMENT_API
    COMMENT_TIMEOUT = 12
    DS_URL = "https://as.xiaohongshu.com/api/sec/v1/ds?appId=xhs-pc-web"

    def __init__(self, http, auth=None):
        RealSource.__init__(self, http, auth)
        self._notes = {}               # note id -> (note, comments, 说明)：详情和评论共用一次请求
        self._cmts = {}                # note id -> (评论表, 拿不到的原因)

    def channels(self):
        return list(self.CHANNELS)

    def image_base(self):
        return self.http.cache.img_dir if self.http.cache is not None else ""

    def _page(self, url, ttl, what, timeout=None):
        raw = self.http.get_bytes(url, ttl=ttl, max_bytes=self.MAX_PAGE_BYTES, timeout=timeout)
        if isinstance(raw, (bytes, bytearray)):
            for enc in ("utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
            return raw.decode("utf-8", "replace")
        return str(raw)

    def _note_url(self, nid, token):
        url = "%s/explore/%s" % (self.base_url, nid)
        if token:
            url += "?xsec_token=%s&xsec_source=pc_feed" % quote(str(token), safe="")
        return url

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.CHANNELS else self.CHANNELS[0]
        html = self._page(self.base_url + "/explore", TTL_LIST, "小红书 推荐", timeout=LIST_TIMEOUT)
        try:
            state = _xhs_state(html)
        except FeedNetError as e:
            # 免登录时拿到的就是一张没有状态块的空壳页——别让人对着"改版了"猜，直接说要 Cookie
            raise FeedNetError("小红书这会儿没给内容（%s）。它要登录：把浏览器 Cookie 贴到 feed_auth.json" % e)
        feeds = ((state.get("feed") or {}).get("feeds") or [])
        posts, videos = [], 0
        for i, it in enumerate(feeds):
            card = it.get("noteCard") if isinstance(it, dict) else None
            if not isinstance(card, dict):
                continue
            if str(card.get("type") or "") == "video":
                videos += 1
                continue
            nid = str(it.get("id") or "")
            title = feed_mod.strip_html(str(card.get("displayTitle") or ""))
            if not nid or not title:
                continue
            user = card.get("user") if isinstance(card.get("user"), dict) else {}
            info = card.get("interactInfo") if isinstance(card.get("interactInfo"), dict) else {}
            try:
                likes = int(str(info.get("likedCount") or "0").replace("+", "") or 0)
            except Exception:
                likes = 0
            cover = _xhs_cover(card.get("cover"))
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=nid,
                title=title,
                author=feed_mod.strip_html(str(user.get("nickName") or "")),
                created="", ts=0,            # 列表页不给发布时间，详情页才给
                score=likes, score_label="赞",
                n_comments=0, comment_label="评论",
                cover=self._cover(cover) if (cover and i < COVER_PER_PAGE) else "",
                excerpt="",
                url=self._note_url(nid, it.get("xsecToken")),
            ))
        if not posts:
            if videos:
                raise FeedNetError("小红书这一页全是视频笔记（这个窗口不放视频），没别的可显示")
            raise FeedNetError("小红书这会儿没给内容（多半是要登录：把浏览器 Cookie 贴到 feed_auth.json）")
        return posts, None

    def _note(self, post):
        """取一条笔记的 `note` 和页面自带的评论（同一份状态，详情和评论共用一次请求）。"""
        nid = str(post.id or "")
        if nid in self._notes and not feed_net.is_fresh():
            return self._notes[nid]
        url = post.url or self._note_url(nid, "")
        if "xsec_token" not in url:
            # 没有 token 的详情地址打不开——列表里给过，这里丢了就只能如实说
            self._notes[nid] = ({}, [], "这条的 xsec_token 丢了（它必须带着才能打开详情）")
            return self._notes[nid]
        html = self._page(url, TTL_DETAIL, "小红书 正文")
        state = _xhs_state(html)
        node = ((state.get("note") or {}).get("noteDetailMap") or {}).get(nid) or {}
        note = node.get("note") if isinstance(node, dict) else None
        comments = ((node.get("comments") or {}).get("list") or []) if isinstance(node, dict) else []
        why = ""
        if not isinstance(note, dict) or not note:
            why = "页面状态里没有这条笔记（多半是 xsec_token 过期了——它是有有效期的）"
            note = {}
        self._notes[nid] = (note, comments, why)
        return self._notes[nid]

    def detail(self, post, cursor=None):
        note, _comments, why = self._note(post)
        out = Post.from_dict(post.to_dict())
        if not note:
            out.body = [Block(kind=BLOCK_QUOTE, text=why or "这条笔记的正文没取到。")]
            return out, None
        user = note.get("user") if isinstance(note.get("user"), dict) else {}
        info = note.get("interactInfo") if isinstance(note.get("interactInfo"), dict) else {}
        if note.get("title"):
            out.title = feed_mod.strip_html(str(note["title"])) or out.title
        if user.get("nickname"):
            out.author = feed_mod.strip_html(str(user["nickname"]))
        # `feed.parse_time` 自己认毫秒（> 1e11 就除 1000），这里不用再动一次
        ts = feed_mod.parse_time(note.get("time"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        if info.get("commentCount") not in (None, ""):
            try:
                out.n_comments = max(0, int(str(info["commentCount"]).replace("+", "") or 0))
            except Exception:
                pass
        if str(info.get("likedCount") or "") != "":
            try:
                out.score = max(0, int(str(info["likedCount"]).replace("+", "") or 0))
            except Exception:
                pass
        tags = [str(t.get("name")) for t in (note.get("tagList") or [])
                if isinstance(t, dict) and t.get("name")]
        if tags:
            out.tags = tags[:6]

        parts = []
        if note.get("desc"):
            parts.append("<p>%s</p>" % _xhs_text(note.get("desc")))
        for img in (note.get("imageList") or [])[:9]:
            if isinstance(img, dict) and (img.get("urlDefault") or img.get("url")):
                parts.append('<img src="%s"/>' % (img.get("urlDefault") or img.get("url")))
        body = self._localize_images(feed_mod.blocks_from_html("".join(parts), max_blocks=20), limit=9)
        # 评论由界面层单独渲染，**不往正文里写"评论在下面（N 条）"**：那是说明文字，
        # 不是笔记内容（真人明确说过前端不要这种话）。取不到就留空，原因进日志。
        cmts, why = self._comments_of(post)
        if why:
            self.last_error = "评论没取到：%s" % why
        out.body = body
        return out, None

    # ------------------------------------------------------------ 评论

    def _web_build(self):
        """Cookie 里的 `webBuild`（它自己前端签名时用的就是它）。"""
        for part in str((self.auth or {}).get("cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == "webBuild":
                    return v.strip()
        return ""

    def _ds_text(self, url):
        """`ds` 那个参数：`as.xiaohongshu.com` 上一段 JS 里写死的 13 位数字。"""
        raw = self.http.get_bytes(url, ttl=TTL_DETAIL, max_bytes=200000, timeout=8)
        if isinstance(raw, (bytes, bytearray)):
            return raw.decode("utf-8", "replace")
        return str(raw)

    def _token_of(self, post):
        m = re.search(r"xsec_token=([^&]+)", post.url or "")
        return unquote(m.group(1)) if m else ""

    def _signed_get(self, path, params):
        """带它那套签名去请求一次（签名见 `xhs_sign.py`）。"""
        cookie = str((self.auth or {}).get("cookie") or "")
        full = xhs_sign.path_with_query(path, params)
        sig = xhs_sign.sign(full, "", cookie, "GET",
                            fetch=self._ds_text, web_build=self._web_build())
        return self.http.get_json("https://edith.xiaohongshu.com" + full, ttl=0,
                                  headers=xhs_sign.headers_for(sig, cookie),
                                  timeout=self.COMMENT_TIMEOUT)

    def _comments_of(self, post):
        """评论 + 拿不到时的原因（详情和评论共用一次请求，失败也算结果，不抛）。"""
        nid = str(post.id or "")
        hit = None if feed_net.is_fresh() else self._cmts.get(nid)
        if hit is not None:
            return hit
        params = {"note_id": nid, "cursor": "", "top_comment_id": "",
                  "image_formats": "jpg,webp,avif"}
        token = self._token_of(post)
        if token:
            params["xsec_token"] = token
        out, why = [], ""
        try:
            obj = self._signed_get(self.COMMENT_API, params)
            out = _xhs_comments(obj)
            if not out:
                why = "这条笔记还没有评论。"
        except FeedNetError as e:
            if "406" in str(e):
                why = "406（签名没过，它可能又换了口径）"
            else:
                why = str(e)
        except Exception as e:                     # 签名/解析出问题也不能把正文一起弄没
            why = str(e)[:80]
        self._cmts[nid] = (out, why)
        return self._cmts[nid]

    def comments(self, post, cursor=None):
        return self._comments_of(post)[0], None


HUPU_BOARDS = (
    # 2026-10-19 逐个抓过一遍：每一条都是 `https://bbs.hupu.com/<slug>`，页面里 40~50 条帖子、
    # 标题取 `<title>`（如 `/nba` → 篮球场）。`/all-*` 那一批是"聚合专题页"，**不是**版面，
    # 里面一条帖子都没有（实测 0 条），所以不能当板块用。
    ("步行街主干道", "bxj"),
    ("篮球场", "nba"),
    ("足球场", "soccer"),
    ("CBA专区", "cba"),
    ("影视区", "ent"),
    ("娱乐圈", "59"),
    ("音乐圈", "63"),
    ("英雄联盟", "lol"),
    ("乒乓球区", "pingpang"),
    ("网球场", "tennis"),
    ("综合体育区", "sports"),
    ("数码综合讨论", "digital"),
    ("运动装备", "gear"),
    ("游戏区", "game"),
    ("历史区", "history"),
    ("恋爱区", "love"),
    ("股票区", "stock"),
    # 真人点名要的：湿乎乎的话题（`/vote`）。它在首页目录里也有，这里放到精选里图个顺手。
    ("湿乎乎的话题", "vote"),
)

# 全量目录（`hupu_boards.py` 是生成物：255 个板，带分类和热度，2026-10-19 抓的）。
# 两个方向各做一张表：`label -> slug` 给"按名字打开板"，`slug -> (分类, 板名, 帖数, 热度)`
# 给"按 slug 打开"和选择器里的显示。
HUPU_SLUG_OF = {}
HUPU_BY_SLUG = {}
for _group, _name, _slug, _count, _heat in hupu_boards.CATALOG:
    HUPU_BY_SLUG.setdefault(_slug, (_group, _name, _count, _heat))
    HUPU_SLUG_OF.setdefault(_name, _slug)
for _label, _slug in HUPU_BOARDS:
    HUPU_SLUG_OF.setdefault(_label, _slug)
    HUPU_BY_SLUG.setdefault(_slug, ("精选", _label, 0, ""))


def _hupu_rows(html):
    """版面列表页 → [{id, title, author, replies, read, time}]。

    虎扑的版面页是**服务端渲染**的（不像帖子页那样带 `__NEXT_DATA__`），所以只能读 HTML。
    一行的骨架是（实测 2026-10-19）：

        <li class="bbs-sl-web-post-body">
          <div class="post-title"><a href="/643069210.html" class="p-title">标题</a></div>
          <div class="post-datum">9 / 899</div>      ← 回复 / 浏览
          <div class="post-auth"><a ...>作者</a></div>
          <div class="post-time">10-19 11:10</div>
        </li>
    """
    out = []
    for seg in str(html or "").split('class="bbs-sl-web-post-body"')[1:]:
        seg = seg.split("</li>", 1)[0]
        a = re.search(r'<a([^>]*class="p-title"[^>]*)>(.*?)</a>', seg, re.S)
        if not a:
            continue
        href = re.search(r'href="/(\d+)\.html"', a.group(1))
        if not href:
            continue
        title = feed_mod.strip_html(a.group(2))
        if not title:
            continue
        datum = re.search(r'class="post-datum"[^>]*>(.*?)</div>', seg, re.S)
        nums = re.findall(r'\d+', feed_mod.strip_html(datum.group(1))) if datum else []
        auth = re.search(r'class="post-auth"[^>]*>(.*?)</div>', seg, re.S)
        tm = re.search(r'class="post-time"[^>]*>(.*?)</div>', seg, re.S)
        out.append({
            "id": href.group(1),
            "title": title,
            "author": feed_mod.strip_html(auth.group(1)) if auth else "",
            "replies": int(nums[0]) if nums else 0,
            "read": int(nums[1]) if len(nums) > 1 else 0,
            "time": feed_mod.strip_html(tm.group(1)) if tm else "",
        })
    return out


def _hupu_time(text):
    """`10-19 11:10` / `11:10` / `昨天 11:10` → epoch 秒；认不出来就给 0（列表里不显示时间）。"""
    s = " ".join(str(text or "").split())
    if not s:
        return 0
    now = time.localtime()
    for fmt, guess in (("%m-%d %H:%M", None), ("%Y-%m-%d %H:%M", None)):
        try:
            t = time.strptime(s, fmt)
        except ValueError:
            continue
        year = t.tm_year if fmt.startswith("%Y") else now.tm_year
        try:
            return time.mktime((year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            return 0
    m = re.match(r"(\d{1,2}):(\d{2})$", s)
    if m:                      # 只有时分 = 今天
        try:
            return time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        except (OverflowError, ValueError):
            return 0
    return 0


class HupuSource(RealSource):
    """虎扑（bbs.hupu.com）。**整站免登录**：版面列表、帖子正文、回复都能直接拿。

    2026-10-19 实测（本机网络，不带任何 Cookie）：
      - 版面页 `/<slug>` 是**服务端渲染**的 HTML（约 300KB），一页 40~50 条帖子，
        每条带标题/作者/回复数/浏览数/时间；翻页是 `/<slug>-2`（`?page=2` **不生效**，
        会把第 1 页原样再发一遍）；
      - 帖子页 `/<tid>.html` 是 Next.js，正文和回复都在 `<script id="__NEXT_DATA__">` 里：
        `props.pageProps.detail.thread`（title / content(HTML) / lights / replies / read /
        createdAt(毫秒) / author.puname）和 `props.pageProps.detail.replies.list`
        （pid / content / allLightCount / createdAt / author.puname）；
      - 那些 `/api/...` 的 JSON 老接口**全 404 了**（`api/topic/list`、`api/thread/detail`、
        `api/v2/recommend/list`、`api.hupu.com` 都试过），所以只能走页面里的数据。

    **板块**：虎扑的"版"就是 URL 里那个 slug，`HUPU_BOARDS` 是逐条抓过的真版面
    （`/all-*` 是聚合专题页，里面一条帖子都没有，不在表里）。换板块走界面里那套选择器。
    """

    key = "hupu"
    label = "虎扑"
    base_url = "https://bbs.hupu.com"
    needs_auth = False               # 免登录
    CHANNELS = tuple(label for label, _slug in HUPU_BOARDS)
    SLUG = dict(HUPU_BOARDS)
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    MAX_PAGE_BYTES = 3000000

    def channels(self):
        """栏目 = 精选那几栏打头，后面跟全量目录（255 个板，同名去重）。

        精选打头是为了"第一次进虎扑落在步行街主干道"、`[`/`]` 之后按 `.` 也是熟悉的那几栏；
        后面接全量目录，真人按 `C` 就能一路往下跳（空板自动跳过，见 main.py 的
        `feed_cycle_channel_skip`），按 `V` 能打字搜任何一个板。"""
        out = list(self.CHANNELS)
        seen = set(out)
        for _group, name, _slug, _count, _heat in hupu_boards.CATALOG:
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def board_fid(self, channel):
        """栏目名 / slug → 虎扑的 slug。认不出来就退回步行街。

        除了精选表和目录，还认"看着像 slug 的串"（`vote`、`bxj`、`lakers`）——这样在
        板块搜索框里直接填一个 slug 也能进；真打错了页面会落回首页（标题里没有板名），
        下一页也就没有帖子，界面上看得很清楚。
        """
        s = str(channel or "").strip()
        if not s:
            return HUPU_BOARDS[0][1]
        if s in HUPU_SLUG_OF:
            return HUPU_SLUG_OF[s]
        if s in HUPU_BY_SLUG:
            return s
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9\-]{1,24}$", s):
            return s
        return HUPU_BOARDS[0][1]

    def board_label(self, fid):
        """slug → 板名（目录里查得到就用真名，查不到就把 slug 原样显示）。"""
        s = str(fid or "").strip()
        hit = HUPU_BY_SLUG.get(s)
        return hit[1] if hit else s

    def accepts_channel(self, channel):
        """这个来源认不认这一栏（选择器里手填的名字/slug 靠它把关）。"""
        s = str(channel or "").strip()
        if not s:
            return False
        return (s in HUPU_SLUG_OF or s in HUPU_BY_SLUG
                or bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9\-]{1,24}$", s)))

    def boards(self, force=False):
        """全量板块表（静态生成物，不联网）：255 个板，带分类和热度。

        `heat` 是虎扑首页给的"帖子总数"文本（如 `18.7w`）；`count` 是同一个数的整数形态。
        `count == 0` 的板子是真的没内容（目录里有 15 个），按 `C` 跳的时候直接跳过。"""
        out = []
        have_label, have_slug = set(), set()
        for label, slug in HUPU_BOARDS:          # 精选那几栏排最前面（名字和 slug 都不重复）
            # 目录里也有这个板（湿乎乎 /vote、篮球场 /nba …）就用目录里的真分类和热度，
            # 别看它是"精选"就当成没有热度——那会让选择器里显示成 `精选 · 湿乎乎的话题`。
            fid = HUPU_SLUG_OF.get(label) or slug      # 目录里同名的那条更权威（bxj -> topic-daily）
            meta = HUPU_BY_SLUG.get(fid) or ()
            if len(meta) == 4 and meta[3]:
                out.append({"label": label, "fid": fid, "group": meta[0],
                            "count": meta[2], "heat": meta[3]})
            else:
                out.append({"label": label, "fid": fid, "group": "精选",
                            "count": 0, "heat": ""})
            have_label.add(label)
            have_slug.add(fid)
        for group, name, slug, count, heat in hupu_boards.CATALOG:
            # 同一个板可能既有"精选名"又有"目录名"（步行街主干道 bxj / topic-daily）：
            # label 或 slug 撞了就不再重复放——选择器里出现两条一模一样的名字最难用。
            if name in have_label or slug in have_slug:
                continue
            out.append({"label": name, "fid": slug, "group": group,
                        "count": count, "heat": heat})
            have_label.add(name)
            have_slug.add(slug)
        return out

    def _headers(self):
        return {"User-Agent": self.UA, "Referer": self.base_url + "/"}

    def _html(self, path, ttl):
        url = path if path.startswith("http") else (self.base_url + path)
        raw = self.http.get_bytes(url, ttl=ttl, headers=self._headers(),
                                  max_bytes=self.MAX_PAGE_BYTES, timeout=LIST_TIMEOUT)
        self.served_stale = bool(getattr(self.http, "served_stale", False))
        return raw.decode("utf-8", "replace")

    def _detail_json(self, tid):
        """帖子页里那坨 `__NEXT_DATA__` 的 `detail` 部分（正文和回复都在里面）。"""
        html = self._html("/%s.html" % str(tid).strip(), TTL_DETAIL)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise FeedNetError("虎扑这一页没带数据（可能改版了）：%s" % tid)
        try:
            data = json.loads(m.group(1))
        except ValueError:
            raise FeedNetError("虎扑页面里的数据读不出来：%s" % tid)
        return ((data.get("props") or {}).get("pageProps") or {}).get("detail") or {}

    def list(self, channel=None, cursor=None):
        label = self.board_label(self.board_fid(channel))
        slug = self.board_fid(channel)
        try:
            page = max(1, int(cursor or 1))
        except (TypeError, ValueError):
            page = 1
        path = "/%s" % slug if page == 1 else "/%s-%d" % (slug, page)
        html = self._html(path, TTL_LIST)
        posts = []
        for row in _hupu_rows(html):
            ts = _hupu_time(row["time"])
            posts.append(Post(
                source=self.key, source_label=self.label, channel=label, pid=row["id"],
                title=row["title"], author=row["author"],
                created=row["time"] or feed_mod.human_time(ts), ts=ts,
                score=row["read"], score_label="浏览",
                n_comments=row["replies"], comment_label="回复",
                url="%s/%s.html" % (self.base_url, row["id"]), tags=[],
            ))
        # 一页 40~50 条；拿到这么多才认为还有下一页（末尾那页会短）
        nxt = page + 1 if len(posts) >= 20 else None
        return posts, nxt

    def detail(self, post, cursor=None):
        out = Post.from_dict(post.to_dict())
        detail = self._detail_json(post.id)
        th = detail.get("thread") or {}
        body = self._localize_images(
            feed_mod.blocks_from_html(th.get("content") or "", max_blocks=60), limit=4)
        if th.get("title"):
            out.title = feed_mod.strip_html(th["title"]) or out.title
        if not body and th.get("video"):
            # 视频帖：Qt 放不了视频，就把地址摆出来（地址本身是内容，不是说明文字）。
            body = [Block(kind=BLOCK_P, text=str(th["video"]))]
        out.body = body
        return out, None

    def comments(self, post, cursor=None):
        """回复（虎扑的回复没有真正的层级，引用写在正文里，所以按平铺给）。"""
        detail = self._detail_json(post.id)
        rows = ((detail.get("replies") or {}).get("list")) or []
        out = []
        for i, it in enumerate(rows):
            blocks = feed_mod.blocks_from_html(it.get("content") or "", max_blocks=12)
            # 没内容的回复就是没内容（以前塞一句"（这条回复没内容）"，属于说明文字）。
            out.append(Comment(
                cid=str(it.get("pid") or ("r%d" % i)),
                author=_hupu_name(it.get("author")) or "匿名",
                created=feed_mod.human_time(int(it.get("createdAt") or 0) / 1000.0),
                score=int(it.get("allLightCount") or 0),
                body=blocks,
                # 虎扑的引用在 `quote` 里（`{"author": {"puname": "..."}, "content": ...}`）：
                # 这就是"这条回复在回谁"，比正文里猜准得多（它们的正文不带 @ 前缀）
                reply_to=_hupu_name((it.get("quote") or {}).get("author")),
            ))
        return out, None


def _hupu_name(author):
    """虎扑的作者字段是 `{"puname": "...", "puid": ...}`（回复和正文同一形状）。"""
    if not isinstance(author, dict):
        return ""
    return str(author.get("puname") or author.get("name") or "").strip()


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

    def comments_page(self, post, cursor=None):
        src = self.by_key.get(getattr(post, "source", ""))
        if src is None:
            return [], None, None
        return src.comments_page(post, cursor)

    def comments(self, post, cursor=None):
        return self._route(post).comments(post, cursor)


def build_real_sources(cache_root=None, config=None, log=None):
    """按配置建好来源。顺序就是界面里 `[`/`]` 的顺序：混流打头，然后 IT之家 / V2EX /
    linux.do / 知乎 / NGA（顺序由下面这张表定，不跟着配置串的顺序走，这样按键顺序永远可预期）。"""
    config = config or {}
    cache = feed_net.Cache(cache_root) if cache_root else None
    auth = load_auth(os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_auth.json"))
    enabled = config.get("feed_sources", None)
    if enabled is None:
        # 配置里没写过 -> 默认这几个；写成空列表 = 用户把它们全关了（只剩离线示例）。
        # 微博不在默认里：它的风控最紧，属于"单独开关"那一档。
        # NGA 和 小红书 也在默认里：它们确实要 Cookie，但没配的时候页脚会照实说
        # "这个来源要 Cookie"，不会假装成功——比"藏起来让人找不到"更好解释。
        enabled = ["ithome", "v2ex", "linuxdo", "zhihu", "nga", "xhs", "hupu",
                   "github", "medium"]
    elif not isinstance(enabled, list):
        enabled = [str(enabled)]

    # 订阅式来源单独住一个文件（feed_more.py）：它们没有评论、没有 Cookie，
    # 跟论坛接口那一套不是一类东西。导入失败只影响这三家，不影响别的来源。
    try:
        import feed_more
        extra = (("github", feed_more.GithubTrendingSource),
                 ("medium", feed_more.RssSource))
    except Exception:
        extra = ()

    made = []
    for key, cls in (("ithome", IthomeSource), ("v2ex", V2exSource),
                     ("linuxdo", DiscourseSource), ("zhihu", ZhihuSource),
                     ("weibo", WeiboSource), ("nga", NgaSource), ("xhs", XhsSource),
                     ("hupu", HupuSource)) + extra:
        if key not in enabled:
            continue
        cred = auth.get(key) if isinstance(auth.get(key), dict) else {}
        http = Http(cache=cache,
                    cookies=_cookies_from(cred.get("cookie")),
                    user_agent=cred.get("user_agent") or feed_net.UA,
                    headers={"Referer": cls.base_url + "/"})
        src = cls(http, cred)
        src.needs_auth = bool(getattr(cls, "needs_auth", False))
        made.append(src)

    # config 里可以自己加订阅（"博客一般都有的那种 RSS/Atom"）：
    #   "feed_rss": ["阮一峰|https://www.ruanyifeng.com/blog/atom.xml",
    #                {"name": "某博客", "url": "https://example.com/feed.xml"}]
    # 每条变成一个独立来源（label 就是名字），键位顺序排在最后；写坏的条目直接跳过。
    # `feed_sources` 写成空列表 = 用户把内置的都关了，这时候也不自作主张把它们加回来。
    if extra and enabled:
        n_rss = 0
        for entry in list(config.get("feed_rss") or [])[:6]:
            try:
                if isinstance(entry, dict):
                    name, url = entry.get("name") or "", entry.get("url") or ""
                else:
                    name, url = str(entry).split("|", 1)
            except Exception:
                continue
            name, url = str(name).strip(), str(url).strip()
            if not (name and url.startswith("http")):
                continue
            n_rss += 1                       # 编号只认"加成功的"，坏条目不留空号
            http = Http(cache=cache, headers={"Referer": url})
            src = feed_more.RssSource(http, {}, feeds=((name, url),),
                                      key="rss%d" % n_rss, label=name)
            src.needs_auth = False
            made.append(src)

    if not made:
        return []
    off = bool(config.get("feed_lowkey", False))
    mix = MixSource(made)
    for src in [mix] + made:
        try:
            src.images_off = off          # 低调模式：封面和正文图都不落地（渲染层画 [图]）
        except Exception:
            pass
    return [mix] + made


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
