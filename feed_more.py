# -*- coding: utf-8 -*-
"""订阅式来源（批次 U）：RSS/Atom、SST 博客、GitHub 热榜。

这三家跟 IT之家 / 知乎那种"论坛接口"不是一类东西：**它们没有评论**。
- 订阅源给的是文章。有的博客把全文放进流里（阮一峰、Better Programming），
  有的只给摘要（Medium 的标签流就是这样）——只给摘要的就摆摘要 + 原文地址，
  绝不把摘要当全文糊满一屏；
- SST 的博客没有订阅地址（rss.xml / feed.xml 全是 403 或 404，页面里也没有
  `<link rel="alternate">`），所以按页面结构抓：列表页的 `<a href="/blog/slug">`
  就是标题，文章正文在 Starlight 的 `.sl-markdown-content` 那个 div 里；
- GitHub 热榜上一行是一个仓库，正文只能是 README（`raw.githubusercontent.com`
  按 `HEAD` 取，免登录、不限流；`api.github.com` 在这台机器上是 403 限流，别用）。

所以 `comments()` 一律返回空列表：界面不会画"评论"那一块（不是"加载失败"）。

三个来源都免登录，只读公开页面和公开流，不碰任何账号。
"""

import html as _html
import re
import time
import xml.etree.ElementTree as ET

import feed as feed_mod
import feed_net
from feed import (Block, Post, BLOCK_CODE, BLOCK_H, BLOCK_IMG, BLOCK_LIST,
                  BLOCK_P, BLOCK_QUOTE)
from feed_net import FeedNetError, TTL_DETAIL, TTL_LIST
from feed_sources import (COVER_PER_PAGE, LIST_TIMEOUT, MAX_PAGES, RealSource,
                          _cookies_from)          # noqa: F401  (_cookies_from 供探针复用)

NOTHING_TO_SAY = ""          # 没有评论时返回的空串（界面据此不画评论那一块）


# --------------------------------------------------------------------------- 公共小工具

def _clean(text):
    """一行纯文本：解 HTML 实体（README 里的 `&middot;`）、合并空白、去掉零宽字符。"""
    s = _html.unescape(str(text or ""))
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _md_inline(text):
    """把行内 Markdown 压成纯文本：链接留字、加粗留字、行内代码留字、徽章丢掉。"""
    s = str(text or "")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)              # 图片（正文里的图另有处理）
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)          # [文字](地址) -> 文字
    s = re.sub(r"<[^>]+>", "", s)                          # 行内 HTML
    s = re.sub(r"(\*\*|__)(.*?)\1", r"\2", s)
    s = re.sub(r"(\*|_)([^*_]{1,80})\1", r"\2", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return _clean(s)


def md_blocks(text, max_blocks=200):
    """Markdown → Block 列表（GitHub 的 README 就是 Markdown）。

    只认最要紧的那几种：标题、段落、列表项、引用、代码块、图片。README 里那些
    徽章（`[![build](badge)](link)`）压完成空字符串就自然消失了，不会变成一排"图"。
    """
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = []
    buf = []
    code = []
    in_code = False

    def flush():
        if buf:
            para = _md_inline(" ".join(buf))
            del buf[:]
            if para:
                out.append(Block(BLOCK_P, para))

    for raw in src.split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            if in_code:
                body = "\n".join(code).strip("\n")
                del code[:]
                in_code = False
                if body:
                    out.append(Block(BLOCK_CODE, body, "", "text"))
            else:
                flush()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if not s:
            flush()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            head = _md_inline(m.group(2))
            if head:
                out.append(Block(BLOCK_H, head))
            continue
        m = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", s)
        if m:
            flush()
            item = _md_inline(m.group(2))
            if item:
                out.append(Block(BLOCK_LIST, item))
            continue
        if s.startswith(">"):
            flush()
            q = _md_inline(s.lstrip("> ").strip())
            if q:
                out.append(Block(BLOCK_QUOTE, q))
            continue
        m = re.match(r"^!\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)$", s)
        if m:
            flush()
            out.append(Block(BLOCK_IMG, "", url=m.group(1)))
            continue
        if s.startswith("<!--") or s.startswith("</") or s.startswith("<"):
            # 一大段 HTML（README 里常见的居中徽章块）：能挤出字就当一个段落，挤不出就丢
            frag = _md_inline(s)
            if frag:
                buf.append(frag)
            continue
        buf.append(s)
        if len(out) >= max_blocks:
            break
    flush()
    if in_code and code:
        body = "\n".join(code).strip("\n")
        if body:
            out.append(Block(BLOCK_CODE, body, "", "text"))
    return out[:max_blocks]


def _first_img(html):
    m = re.search(r'<img[^>]+src="([^"]+)"', str(html or ""), re.I)
    return m.group(1) if m else ""


def _extract_div(html, marker, limit=600000):
    """从 `marker` 往回找它所在的那个 `<div>`，再按层数配对到闭合标签。"""
    text = str(html or "")
    i = text.find(marker)
    if i < 0:
        return ""
    start = text.rfind("<div", 0, i)
    if start < 0:
        return ""
    depth = 0
    for m in re.finditer(r"<div\b|</div>", text[start:start + limit]):
        if m.group(0) == "</div>":
            depth -= 1
            if depth <= 0:
                return text[start:start + m.end()]
        else:
            depth += 1
    return text[start:start + limit]


# --------------------------------------------------------------------------- RSS / Atom

_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
}


def _local(tag):
    return str(tag or "").split("}")[-1].lower()


def _txt(el):
    return _clean("".join(el.itertext())) if el is not None else ""


def _child(el, *names):
    for name in names:
        for kid in list(el):
            if _local(kid.tag) == name:
                return kid
    return None


def _link_of(el):
    """RSS 的 `<link>文字</link>` 和 Atom 的 `<link rel="alternate" href="..."/>` 都要认。"""
    best = ""
    for kid in list(el):
        if _local(kid.tag) != "link":
            continue
        href = (kid.get("href") or "").strip()
        if href:
            rel = (kid.get("rel") or "alternate").strip().lower()
            if rel in ("alternate", "") and not best:
                best = href
            elif not best:
                best = href
        else:
            txt = _txt(kid)
            if txt:
                best = txt
    return best


def parse_feed(raw):
    """RSS 2.0 / Atom 都能吃的解析器：返回 [{title, link, guid, date, author, tags, html}]。

    只认结构、不认具体站点——所以"博客一般都有的订阅格式"可以直接往里塞。
    """
    out = []
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        raise FeedNetError("订阅流解析失败（%s）" % type(e).__name__)
    nodes = []
    for el in root.iter():
        if _local(el.tag) in ("item", "entry"):
            nodes.append(el)
    for el in nodes:
        title = _txt(_child(el, "title"))
        link = _link_of(el)
        guid = _txt(_child(el, "guid", "id"))
        date = _txt(_child(el, "pubdate", "published", "updated", "date"))
        author = _txt(_child(el, "creator", "author"))
        tags = [_txt(k) for k in list(el) if _local(k.tag) == "category"]
        # 正文：content:encoded 最全，其次是 Atom 的 content，最后才是摘要
        html = ""
        full = False
        for name in ("encoded", "content"):
            kid = _child(el, name)
            if kid is not None:
                cand = "".join(kid.itertext())
                if len(cand) > len(html):
                    html, full = cand, True
        kid = _child(el, "description", "summary")
        if kid is not None:
            cand = "".join(kid.itertext())
            if len(cand) > len(html):
                html, full = cand, False
        out.append({"title": title, "link": link, "guid": guid, "date": date,
                    "author": author, "tags": [t for t in tags if t],
                    "html": html, "full": full})
    return out


def _pid_of(item):
    """稳定 id：guid 优先，没有就用地址的哈希（列表和详情两次取数必须对得上）。"""
    raw = str(item.get("guid") or item.get("link") or item.get("title") or "")
    if not raw:
        return ""
    import hashlib
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


class RssSource(RealSource):
    """通用订阅源。`FEEDS` 就是它的"板块"：一个订阅地址一个板块。

    Medium 在这里是两条：Better Programming 的流里带全文，Medium 官方的标签流只带摘要
    （`full=False`）——摘要那种进详情只摆摘要 + 原文地址，不假装有全文。
    """

    key = "medium"
    label = "Medium"
    base_url = "https://medium.com"
    needs_auth = False
    PER_PAGE = 10
    FEEDS = (
        ("Better Programming", "https://betterprogramming.pub/feed"),
        ("编程", "https://medium.com/feed/tag/programming"),
        ("科技", "https://medium.com/feed/tag/technology"),
        ("AI", "https://medium.com/feed/tag/artificial-intelligence"),
    )

    def __init__(self, http, auth=None, feeds=None, key=None, label=None, base_url=None):
        RealSource.__init__(self, http, auth)
        self.FEEDS = tuple(feeds or self.FEEDS)
        if key:
            self.key = key
        if label:
            self.label = label
        if base_url:
            self.base_url = base_url
        self._memo = {}

    def channels(self):
        return [name for name, _url in self.FEEDS]

    def feed_url(self, channel):
        for name, url in self.FEEDS:
            if name == channel:
                return url
        return self.FEEDS[0][1] if self.FEEDS else ""

    def _items(self, channel):
        url = self.feed_url(channel)
        if not url:
            raise FeedNetError("这个来源还没有配订阅地址")
        key = (self.key, channel, url)
        hit = self._memo.get(key)
        if hit and (time.time() - hit[0]) < 120 and not feed_net.is_fresh():
            return hit[1]
        raw = self.http.get_bytes(url, ttl=TTL_LIST, max_bytes=4000000, timeout=LIST_TIMEOUT + 6)
        items = parse_feed(raw)
        self._memo[key] = (time.time(), items)
        return items

    def list(self, channel=None, cursor=None):
        channels = self.channels()
        channel = channel if channel in channels else (channels[0] if channels else None)
        page = int(cursor or 0)
        items = self._items(channel)
        posts = []
        for i, it in enumerate(items):
            title = it["title"]
            pid = _pid_of(it)
            if not (title and pid):
                continue
            html = it.get("html") or ""
            ts = feed_mod.parse_time(it.get("date"))
            cover = ""
            if i < COVER_PER_PAGE:
                img = _first_img(html)
                if img:
                    cover = self._cover(img)
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=pid,
                title=title, author=it.get("author") or "",
                created=feed_mod.human_time(ts) if ts else "",
                ts=ts or 0, url=it.get("link") or "",
                excerpt=feed_mod.strip_html(html)[:200],
                cover=cover, tags=list(it.get("tags") or [])[:4],
            ))
        start = page * self.PER_PAGE
        chunk = posts[start:start + self.PER_PAGE]
        nxt = start + self.PER_PAGE if (chunk and start + self.PER_PAGE < len(posts)
                                        and page + 1 < MAX_PAGES) else None
        return chunk, nxt

    def detail(self, post, cursor=None):
        out = Post.from_dict(post.to_dict())
        item = None
        for it in self._items(post.channel or self.channels()[0]):
            if _pid_of(it) == post.id:
                item = it
                break
        if item is None:
            raise FeedNetError("这条在流里已经翻过去了（订阅流只留最近几十条）")
        blocks = feed_mod.blocks_from_html(item.get("html") or "", max_blocks=260)
        out.body = self._localize_images(blocks, limit=6)
        if item.get("author"):
            out.author = item["author"]
        ts = feed_mod.parse_time(item.get("date"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        if item.get("link"):
            out.url = item["link"]
        if item.get("tags"):
            out.tags = list(item["tags"])[:4]
        if not item.get("full") and out.url:
            # 订阅流只给了摘要：正文末尾补一行原文地址（不是说明文字，是地址本身）
            out.body = list(out.body) + [Block(BLOCK_P, out.url)]
        return out, None

    def comments(self, post, cursor=None):
        return [], NOTHING_TO_SAY


class SstSource(RealSource):
    """SST 的博客（sst.dev/blog）。没有订阅地址，按页面结构抓：

    - 列表页里 `<a href="/blog/<slug>">标题</a>` 就是一条，顺序即页面顺序（新在前）；
    - 文章页的正文在 Starlight 的 `.sl-markdown-content` 那个 div 里，`<h1>` 是标题。
    """

    key = "sst"
    label = "SST"
    base_url = "https://sst.dev"
    needs_auth = False
    LIST_URL = "https://sst.dev/blog/"
    PER_PAGE = 12
    LINK_RE = re.compile(r'<a[^>]+href="/blog/([a-z0-9][a-z0-9\-]*)"[^>]*>(.*?)</a>',
                         re.S | re.I)

    def channels(self):
        return ["博客"]

    def list(self, channel=None, cursor=None):
        page = int(cursor or 0)
        raw = self.http.get_bytes(self.LIST_URL, ttl=TTL_LIST, max_bytes=3000000,
                                 timeout=LIST_TIMEOUT + 6).decode("utf-8", "replace")
        seen = []
        for slug, inner in self.LINK_RE.findall(raw):
            if any(slug == s for s, _t in seen):
                continue
            title = feed_mod.strip_html(inner).strip()
            if not title or title.lower() in ("blog", "docs", "examples"):
                continue
            seen.append((slug, title))
        posts = [Post(source=self.key, source_label=self.label, channel="博客", pid=slug,
                      title=title, url="%s/blog/%s" % (self.base_url.rstrip("/"), slug))
                 for slug, title in seen]
        posts = [p for p in posts if p.id and p.title]
        start = page * self.PER_PAGE
        chunk = posts[start:start + self.PER_PAGE]
        nxt = start + self.PER_PAGE if (chunk and start + self.PER_PAGE < len(posts)
                                        and page + 1 < MAX_PAGES) else None
        return chunk, nxt

    def detail(self, post, cursor=None):
        out = Post.from_dict(post.to_dict())
        url = post.url or ("%s/blog/%s" % (self.base_url.rstrip("/"), post.id))
        raw = self.http.get_bytes(url, ttl=TTL_DETAIL, max_bytes=3000000,
                                  timeout=LIST_TIMEOUT + 10).decode("utf-8", "replace")
        m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.S | re.I)
        title = feed_mod.strip_html(m.group(1)).strip() if m else ""
        if not title:
            m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', raw, re.I)
            title = _clean(m.group(1)) if m else ""
        if title:
            out.title = title.split(" | ")[0].strip() or out.title
        body_html = _extract_div(raw, 'class="sl-markdown-content')
        if body_html:
            blocks = feed_mod.blocks_from_html(body_html, max_blocks=260)
        else:
            blocks = []
        if not blocks:
            raise FeedNetError("文章正文没找到（页面结构可能又变了）")
        out.body = self._localize_images(blocks, limit=6)
        out.url = url
        return out, None

    def comments(self, post, cursor=None):
        return [], NOTHING_TO_SAY


class GithubTrendingSource(RealSource):
    """GitHub 热榜（github.com/trending）。**每日/每周/每月**三个板块。

    正文是仓库的 README（`raw.githubusercontent.com/<owner>/<repo>/HEAD/README.md`）。
    `api.github.com` 在这台机器上直接 403（限流），所以热榜那一页也自己解析 HTML，
    不走 API——这也省掉了 token。
    """

    key = "github"
    label = "GitHub 热榜"
    base_url = "https://github.com"
    needs_auth = False
    SINCE = (("今日", "daily"), ("本周", "weekly"), ("本月", "monthly"))
    PER_PAGE = 12
    ROW_RE = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
    REPO_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="/([^"/]+/[^"]+)"', re.S)
    DESC_RE = re.compile(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', re.S)
    LANG_RE = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
    STARS_RE = re.compile(r'([\d,]+)\s+stars?\s+(today|this week|this month)')
    TOTAL_RE = re.compile(r'href="/[^"]+/stargazers"[^>]*>.*?</svg>\s*([\d,]+)\s*</a>', re.S)
    FORK_RE = re.compile(r'href="/[^"]+/forks"[^>]*>.*?</svg>\s*([\d,]+)\s*</a>', re.S)

    def channels(self):
        return [name for name, _s in self.SINCE]

    def _since(self, channel):
        for name, since in self.SINCE:
            if name == channel:
                return since
        return self.SINCE[0][1]

    def list(self, channel=None, cursor=None):
        channels = self.channels()
        channel = channel if channel in channels else channels[0]
        page = int(cursor or 0)
        url = "%s/trending?since=%s" % (self.base_url, self._since(channel))
        raw = self.http.get_bytes(url, ttl=TTL_LIST, max_bytes=4000000,
                                  timeout=LIST_TIMEOUT + 8).decode("utf-8", "replace")
        posts = []
        for row in self.ROW_RE.findall(raw):
            m = self.REPO_RE.search(row)
            if not m:
                continue
            full = m.group(1).strip()
            desc = self.DESC_RE.search(row)
            lang = self.LANG_RE.search(row)
            stars = self.STARS_RE.search(row)
            total = self.TOTAL_RE.search(row)
            forks = self.FORK_RE.search(row)
            n_today = int((stars.group(1) if stars else "0").replace(",", "") or 0)
            n_total = int((total.group(1) if total else "0").replace(",", "") or 0)
            n_fork = int((forks.group(1) if forks else "0").replace(",", "") or 0)
            tags = []
            if lang:
                tags.append(_clean(lang.group(1)))
            if n_total:
                tags.append("%s star" % _num(n_total))
            if n_fork:
                tags.append("%s fork" % _num(n_fork))
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=full,
                title=full,
                author=full.split("/")[0],
                excerpt=feed_mod.strip_html(desc.group(1)) if desc else "",
                score=n_today, score_label="star", n_comments=0,
                url="%s/%s" % (self.base_url, full),
                tags=tags,
            ))
        start = page * self.PER_PAGE
        chunk = posts[start:start + self.PER_PAGE]
        nxt = start + self.PER_PAGE if (chunk and start + self.PER_PAGE < len(posts)
                                        and page + 1 < MAX_PAGES) else None
        return chunk, nxt

    def _readme(self, full):
        """README：先按 `HEAD` 试几个常见文件名；拿到的是"再做一次"的跳转行也跟着走一次。"""
        for name in ("README.md", "readme.md", "README.markdown", "README.rst", "README"):
            url = "https://raw.githubusercontent.com/%s/HEAD/%s" % (full, name)
            try:
                raw = self.http.get_bytes(url, ttl=TTL_DETAIL, max_bytes=1500000,
                                          timeout=LIST_TIMEOUT + 8)
            except Exception:
                continue
            text = raw.decode("utf-8", "replace")
            body = text.strip()
            if len(body) < 60 and re.match(r"^[\w./\-]+\.(md|markdown|rst)$", body):
                # 有的仓库根 README 只是一行指向真正的 README
                try:
                    raw2 = self.http.get_bytes(
                        "https://raw.githubusercontent.com/%s/HEAD/%s" % (full, body),
                        ttl=TTL_DETAIL, max_bytes=1500000, timeout=LIST_TIMEOUT + 8)
                    text = raw2.decode("utf-8", "replace")
                except Exception:
                    pass
            return text, name
        return "", ""

    def detail(self, post, cursor=None):
        out = Post.from_dict(post.to_dict())
        full = str(post.id or "")
        if "/" not in full:
            raise FeedNetError("这不是一个仓库名")
        text, name = self._readme(full)
        if not text.strip():
            raise FeedNetError("这个仓库没有取到 README")
        if name.endswith(".rst"):
            body = [Block(BLOCK_P, line.strip()) for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("..")]
        else:
            body = md_blocks(text, max_blocks=220)
        blocks = self._localize_images(body, limit=6)
        blocks = [b for b in blocks if not (b.kind == BLOCK_P and not (b.text or "").strip())]
        out.body = blocks or [Block(BLOCK_P, "这个仓库的 README 是空的（或者全是被过滤掉的徽章）。")]
        return out, None

    def comments(self, post, cursor=None):
        return [], NOTHING_TO_SAY


def _num(n):
    return "{:,}".format(int(n or 0))
