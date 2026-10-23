# -*- coding: utf-8 -*-
"""社区统一流：数据模型 + 来源适配器 + HTML 渲染 + 导航逻辑。

设计见 docs/community-feed-design.md：**所有来源只写一个适配器**（列表 / 正文 / 评论三件事），
界面层只认 Post / Comment / Block 这三个结构，所以加一个来源 = 加一个适配器，界面一行不改。

分层（批次 B 起）：
- `feed.py`（本文件）：模型 / 排版 / 导航，外加"HTML 正文 → Block"这个所有来源都要用的转换器；
- `feed_net.py`：网络 + 磁盘缓存 + 图片落地（超时、限速、失败兜底都在那）；
- `feed_sources.py`：每个站一个适配器 + 混流适配器。
本文件**导入时不发任何请求**，渲染也绝不发请求——取数只发生在 main.py 的后台线程里。

批次 A 只有 DemoSource（读 feed_demo/ 里的 JSON）：零配置、离线、给真人看效果用。
批次 B 加了 IT之家 / V2EX / linux.do（Discourse）三个真实来源 + 混流。
"""

import colorsys
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

# 正文块的种类（Block.kind）
BLOCK_P = "p"
BLOCK_H = "h"
BLOCK_QUOTE = "quote"
BLOCK_CODE = "code"
BLOCK_IMG = "img"
BLOCK_LIST = "list"
BLOCK_TABLE = "table"
KINDS = (BLOCK_P, BLOCK_H, BLOCK_QUOTE, BLOCK_CODE, BLOCK_IMG, BLOCK_LIST, BLOCK_TABLE)

PAGE_SIZE = 6          # 演示源每页几条（用来跑通"快到底自动续页"这条路）


# --------------------------------------------------------------------------- 数据模型

class Block(object):
    """正文里的一个块。来源千差万别（知乎是富文本、V2EX 是纯文本、微博是图文），
    统一成这几种，渲染层就只写一遍。"""

    __slots__ = ("kind", "text", "url", "lang")

    def __init__(self, kind=BLOCK_P, text="", url="", lang=""):
        self.kind = kind if kind in KINDS else BLOCK_P
        self.text = text or ""
        self.url = url or ""
        self.lang = lang or ""

    @classmethod
    def from_dict(cls, d):
        if isinstance(d, Block):        # 适配器直接给 Block 也收（批次 B 的每个来源都这么写）
            return cls(d.kind, d.text, d.url, d.lang)
        if isinstance(d, str):          # 容错：来源只给了纯文本
            return cls(BLOCK_P, d)
        d = d or {}
        return cls(d.get("kind", BLOCK_P), d.get("text", ""), d.get("url", ""), d.get("lang", ""))

    def to_dict(self):
        return {"kind": self.kind, "text": self.text, "url": self.url, "lang": self.lang}


class Comment(object):
    """一条评论（带子树）。

    深度不写死在数据里，由 comment_forest 按树算出来——来源给的层级结构各不相同，
    统一在渲染前算好，就不会每个来源一套缩进。"""

    __slots__ = ("id", "author", "created", "score", "body", "replies", "reply_to", "more_for",
                 "n_replies")

    def __init__(self, cid, author="", created="", score=0, body=None, replies=None,
                 reply_to="", more_for="", n_replies=0):
        self.id = str(cid)
        self.author = author or ""
        self.created = created or ""
        self.score = int(score or 0)
        self.body = [Block.from_dict(b) for b in (body or [])]
        self.replies = list(replies or [])
        # 来源**明确知道**这条回复的是谁时（虎扑的 quote、Discourse 的 reply_to_post_number 之类）
        # 直接填这里；不知道就留空，由 FeedNav.reply_to_of 从层级或正文开头的 `@某某` 推。
        self.reply_to = reply_to or ""
        # 非空 = 这行是"还有 N 条回复"的占位（`cid` 指向父评论），不是真评论
        self.more_for = more_for or ""
        # 站上这条评论**总共**有多少条回复（0 = 不知道，就按本地取到的算）。
        # 小红书首页只给 1 条预览，`sub_comment_count` 却写着 33——不记这个数，
        # "还有 N 条回复"就会算成 0，人也点不出更多的来。
        self.n_replies = int(n_replies or 0)

    @classmethod
    def from_dict(cls, d):
        if isinstance(d, Comment):      # 同上：适配器直接给 Comment 也收
            return cls(d.id, d.author, d.created, d.score, d.body, d.replies,
                       getattr(d, "reply_to", ""), getattr(d, "more_for", ""),
                       getattr(d, "n_replies", 0))
        d = d or {}
        return cls(
            d.get("id", ""), d.get("author", ""), d.get("created", ""), d.get("score", 0),
            d.get("body", []),
            [cls.from_dict(r) for r in (d.get("replies") or [])],
            d.get("reply_to", ""), d.get("more_for", ""),
            d.get("n_replies", 0),
        )

    def to_dict(self):
        return {
            "id": self.id, "author": self.author, "created": self.created, "score": self.score,
            "body": [b.to_dict() for b in self.body],
            "replies": [r.to_dict() for r in self.replies],
            "reply_to": self.reply_to,
            "more_for": self.more_for,
            "n_replies": self.n_replies,
        }

    def total(self):
        """自己 + 所有后代。折叠时页脚要显示"这里还藏着几条"。"""
        return 1 + sum(r.total() for r in self.replies)


class Post(object):
    """一条帖子。列表页只用前半截字段，详情页才有 body；图片一律走本地缓存目录。"""

    __slots__ = ("source", "source_label", "channel", "id", "title", "author", "created",
                 "score", "score_label", "n_comments", "comment_label", "cover", "excerpt",
                 "url", "body", "tags", "comments", "ts")

    def __init__(self, source="", channel="", pid="", title="", author="", created="", score=0,
                 n_comments=0, cover="", excerpt="", url="", body=None, tags=None, comments=None,
                 source_label="", ts=0, score_label="", comment_label=""):
        self.source = source
        self.source_label = source_label or source
        self.channel = channel or ""
        self.id = str(pid)
        self.title = title or ""
        self.author = author or ""
        self.created = created or ""
        self.score = int(score or 0)
        # 统计数字的**单位**：知乎给的是"热度"不是赞，IT之家给的是阅读量不是赞。
        # 同一个数字换个来源含义就变了，所以单位跟着帖子走，不写死在渲染层。
        self.score_label = score_label or ""
        self.n_comments = int(n_comments or 0)
        self.comment_label = comment_label or ""
        self.cover = cover or ""          # 相对图片来源目录的文件名
        self.excerpt = excerpt or ""
        self.url = url or ""
        self.body = [Block.from_dict(b) for b in (body or [])]
        self.tags = list(tags or [])
        self.comments = [Comment.from_dict(c) for c in (comments or [])]
        self.ts = float(ts or 0)          # 发布时刻（epoch 秒）：混流排序、相对时间都靠它

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            source=d.get("source", ""), source_label=d.get("source_label", ""),
            channel=d.get("channel", ""), pid=d.get("id", ""), title=d.get("title", ""),
            author=d.get("author", ""), created=d.get("created", ""), score=d.get("score", 0),
            n_comments=d.get("n_comments", 0), cover=d.get("cover", ""),
            excerpt=d.get("excerpt", ""), url=d.get("url", ""), body=d.get("body", []),
            tags=d.get("tags", []), comments=d.get("comments", []), ts=d.get("ts", 0),
            score_label=d.get("score_label", ""), comment_label=d.get("comment_label", ""),
        )

    def to_dict(self):
        return {
            "source": self.source, "source_label": self.source_label, "channel": self.channel,
            "id": self.id, "title": self.title, "author": self.author, "created": self.created,
            "score": self.score, "n_comments": self.n_comments, "cover": self.cover,
            "score_label": self.score_label, "comment_label": self.comment_label,
            "excerpt": self.excerpt, "url": self.url, "ts": self.ts,
            "body": [b.to_dict() for b in self.body], "tags": self.tags,
            "comments": [c.to_dict() for c in self.comments],
        }

    def text_length(self):
        return sum(len(b.text) for b in self.body)


def comment_forest(tree):
    """把评论树拍平成 [(comment, depth, parent)]，顺序就是显示顺序。

    上下移动（J/K）、折叠、页脚统计都在这条序列上做，所以它必须是**唯一的顺序来源**。"""
    out = []

    def walk(nodes, depth, parent=None):
        for c in nodes:
            out.append((c, depth, parent))      # 父节点留着：表头要写"回复 X"
            walk(c.replies, depth + 1, c)

    walk(tree or [], 0)
    return out


def descendant_ids(node):
    out = []
    for r in node.replies:
        out.append(r.id)
        out.extend(descendant_ids(r))
    return out


# --------------------------------------------------------------------------- 通用转换

def parse_time(value):
    """各种时间写法 → epoch 秒。认不出来就返回 0（排序时排最后，不会瞎编时间）。"""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        # 有的站给毫秒时间戳，超过这个量级就当毫秒
        return v / 1000.0 if v > 1e11 else v
    text = str(value).strip()
    if not text:
        return 0.0
    if re.match(r"^\d+(\.\d+)?$", text):
        return parse_time(float(text))
    iso = text.replace("Z", "+00:00")
    # "2026-10-15T16:11:03.047"（IT之家）这种没有时区，当本地时间更贴近真人的感觉
    for candidate in (iso, text.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                return dt.timestamp()
            return dt.astimezone().timestamp()
        except Exception:
            continue
    # 订阅流（RSS 2.0 / Atom）的日期是 RFC 822 那种写法：`Mon, 19 Oct 2026 08:38:36 GMT`。
    # 交给标准库解——自己写 strptime 表会漏掉时区名和两位数年份。
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(text)
        if dt is not None:
            return dt.timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except Exception:
            continue
    return 0.0


def human_time(ts, now=None):
    """相对时间：列表页写"3 小时前"比写 "2026-10-15T16:11" 好读得多。"""
    try:
        ts = float(ts or 0)
    except Exception:
        return ""
    if ts <= 0:
        return ""
    now = time.time() if now is None else now
    d = now - ts
    if d < -60:
        return "刚刚"
    if d < 60:
        return "刚刚"
    if d < 3600:
        return "%d 分钟前" % int(d // 60)
    if d < 86400:
        return "%d 小时前" % int(d // 3600)
    dt = datetime.fromtimestamp(ts)
    if d < 172800:
        return "昨天 %02d:%02d" % (dt.hour, dt.minute)
    if dt.year == datetime.fromtimestamp(now).year:
        return "%02d-%02d %02d:%02d" % (dt.month, dt.day, dt.hour, dt.minute)
    return "%04d-%02d-%02d" % (dt.year, dt.month, dt.day)


class _BlocksHTMLParser(HTMLParser):
    """HTML → Block 列表。用标准库（不引 bs4/lxml），IT之家和 Discourse 共用一套。"""

    BLOCK = {"p": BLOCK_P, "div": BLOCK_P, "section": BLOCK_P, "article": BLOCK_P,
             "main": BLOCK_P, "table": BLOCK_P, "tr": BLOCK_P, "td": BLOCK_P,
             "figure": BLOCK_P, "figcaption": BLOCK_P, "dt": BLOCK_P, "dd": BLOCK_P,
             "blockquote": BLOCK_QUOTE, "pre": BLOCK_CODE,
             "h1": BLOCK_H, "h2": BLOCK_H, "h3": BLOCK_H, "h4": BLOCK_H,
             "h5": BLOCK_H, "h6": BLOCK_H, "li": BLOCK_LIST}
    SKIP = ("script", "style", "head", "title", "noscript", "svg", "iframe", "button",
            "form", "input", "select", "template", "nav", "footer", "aside")
    # 这些标签没有结束标签，不能靠 handle_endtag 把 skip 计数减回来
    SKIP_VOID = ("input",)

    def __init__(self, max_blocks=400):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.out = []
        self.buf = []
        self.kind = BLOCK_P
        self.stack = []
        self.stack_via = []     # 与 stack 同步：每层是"怎么进来的"
        self.skip = 0
        self.pre = 0
        self.max_blocks = max_blocks

    # -- 内部 -------------------------------------------------------------
    def flush(self):
        text = "".join(self.buf)
        self.buf = []
        if self.pre:
            text = text.strip("\n")
            while "\n\n\n" in text:
                text = text.replace("\n\n\n", "\n\n")
        else:
            text = re.sub(r"[ \t\r\f\v]+", " ", text)
            text = re.sub(r"\s*\n\s*", " ", text)
        text = text.strip()
        if text:
            self.out.append(Block(self.kind, text))
        if len(self.out) >= self.max_blocks:
            self.buf = []

    def add_img(self, raw_attrs):
        src = _pick_img_src(raw_attrs)
        if src:
            self.flush()
            if len(self.out) < self.max_blocks:
                self.out.append(Block(BLOCK_IMG, "", url=src))

    # -- HTMLParser 回调 ---------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag in self.SKIP:
            if tag not in self.SKIP_VOID:
                self.skip += 1
            return
        if self.skip:
            return
        if tag == "img":
            self.add_img(attrs)
            return
        if tag == "br":
            self.buf.append("\n")
            return
        if tag in self.BLOCK:
            self.flush()
            self.stack.append(self.kind)
            self.kind = self.BLOCK[tag]
            if tag == "pre":
                self.pre += 1
            return
        # 行内标签都不用管：文本自己会流进 buf

    def handle_startendtag(self, tag, attrs):
        if (tag or "").lower() == "img":
            self.add_img(attrs)
        elif (tag or "").lower() == "br":
            self.buf.append("\n")

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in self.BLOCK:
            if tag == "pre" and self.pre:
                self.pre -= 1
            self.flush()
            self.kind = self.stack.pop() if self.stack else BLOCK_P

    def handle_data(self, data):
        if self.skip or not data or len(self.out) >= self.max_blocks:
            return
        self.buf.append(data)

    def finish(self):
        self.flush()
        return self.out


def _pick_img_src(attrs):
    """留内容图、丢头像/表情/图标——表情图在正文里就是一行小图片，纯噪音。"""
    a = {}
    for k, v in (attrs or []):
        a[(k or "").lower()] = v or ""
    src = a.get("src") or a.get("data-src") or a.get("data-original") or a.get("data-lazy-src") or ""
    cls = (a.get("class") or "").lower()
    if not src or src.lower().startswith("data:"):
        return ""
    if any(k in cls for k in ("emoji", "avatar", "smiley", "icon")) or "avatar" in (a.get("alt") or "").lower():
        return ""
    if re.search(r"(emoji|avatar|smiley|/icon)", src, re.I):
        return ""
    for key in ("width", "w", "data-w"):
        try:
            w = int(float(a.get(key) or 0))
        except Exception:
            w = 0
        if w and w <= 48:
            return ""
    return src


def blocks_from_html(text, max_blocks=400):
    """HTML 正文 → Block 列表。任何异常都退化成一个纯文本段落（正文绝不能因为解析炸掉）。"""
    if not text:
        return []
    try:
        p = _BlocksHTMLParser(max_blocks=max_blocks)
        p.feed(str(text))
        return p.finish()
    except Exception:
        plain = strip_html(str(text))
        return [Block(BLOCK_P, plain)] if plain else []


def strip_html(text):
    """HTML → 一行纯文本（摘要、评论预览、备份都用它）。"""
    if not text:
        return ""
    try:
        p = _BlocksHTMLParser(max_blocks=600)
        p.feed(str(text))
        p.finish()
        parts = [b.text for b in p.out if b.kind != BLOCK_IMG and b.text]
        return re.sub(r"\s+", " ", " ".join(parts)).strip()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text))).strip()


def merge_posts(post_lists, limit=0):
    """混流：把各来源的列表合并成一条。

    - 按 (时间倒序, 热度倒序) 排；没有时间的排最后（不瞎编时间）；
    - (来源, id) 去重：同一页被拉两次也不会出现两条；
    - 标题为空的丢掉（导航和书签都靠标题认人）。"""
    seen = set()
    out = []
    for lst in (post_lists or []):
        for p in (lst or []):
            if not p or not (p.title or "").strip():
                continue
            key = (p.source, p.id)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    out.sort(key=lambda p: (p.ts or 0, p.score or 0), reverse=True)
    if limit:
        return out[:limit]
    return out


# --------------------------------------------------------------------------- 来源适配器

class SourceAdapter(object):
    """一个来源要实现的全部东西（批次 B 起每个站一个子类）。

    `list/detail/comments` 都返回 (数据, 下一个游标)：游标为 None 表示没有更多了。
    取数**永远在后台线程里**跑（main.py 那边负责），这里只管拿数据、不管线程和界面。"""

    key = ""
    label = ""
    needs_auth = False          # 带 Cookie 的来源置 True：界面要提示、设置里显示"已配置/未配置"

    def list(self, channel=None, cursor=None):
        raise NotImplementedError

    def detail(self, post, cursor=None):
        raise NotImplementedError

    def comments(self, post, cursor=None):
        raise NotImplementedError

    def comments_page(self, post, cursor=None):
        """带游标的评论分页：(评论列表, 失败原因, 下一个游标)。

        默认实现 = "一次给完"（游标永远 None）：能翻页的来源自己重写这一个方法，
        其余来源照旧只实现 `comments()`，页面那边就只是把已经取回来的分几次摆出来。"""
        try:
            items, why = self.comments(post, cursor)
        except TypeError:
            # 老适配器的 `comments(post)` 没有 cursor 参数（离线夹具就是这样）：退回两参数调
            try:
                items, why = self.comments(post)
            except Exception as e:
                return [], type(e).__name__, None
        except Exception as e:
            return [], type(e).__name__, None
        return items, why, None

    def channels(self):
        """来源内的栏目（Reddit 的 subreddit 那个位置）。"""
        return []

    def image_base(self):
        """图片解析基准目录（本地路径）。main.py 拿它当 QTextDocument 的 baseUrl。"""
        return ""


class DemoSource(SourceAdapter):
    """离线演示源：读 feed_demo/posts.json，一个网络请求都不发。

    两个理由：① 装完就能看见社区页长什么样（不用配任何 Cookie、不用科学上网）；
    ② 探针拿它当夹具——验收不依赖网络、不碰真人账号。"""

    key = "demo"
    label = "示例（离线）"
    needs_auth = False

    def __init__(self, root):
        self.root = root
        self._data = None

    def _load(self):
        if self._data is None:
            with open(os.path.join(self.root, "posts.json"), "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._data = [Post.from_dict(d) for d in (raw.get("posts") or [])]
        return self._data

    def channels(self):
        """栏目列表由**来源自己**声明（"全部"要不要算一栏，只有来源知道）。

        演示源支持"全部"（list() 里认这个值），所以它自己把它放在第一个；
        真实来源大多只有一两个固定栏目，就不该凭空多出一栏——否则按 `,` 会在
        "全部"和一个等同的栏目之间来回跳，看着像坏了。"""
        seen = ["全部"]
        for p in self._load():
            label = p.channel or "默认"
            if label not in seen:
                seen.append(label)
        return seen

    def list(self, channel=None, cursor=None):
        posts = self._load()
        if channel and channel != "全部":
            posts = [p for p in posts if (p.channel or "默认") == channel]
        start = int(cursor or 0)
        page = posts[start:start + PAGE_SIZE]
        nxt = start + PAGE_SIZE if start + PAGE_SIZE < len(posts) else None
        # 列表页不带正文/评论（真实来源那边是真的不取；这里照做，免得渲染层养成依赖）
        out = []
        for p in page:
            q = Post.from_dict(p.to_dict())
            q.body, q.comments = [], []
            out.append(q)
        return out, nxt

    def detail(self, post, cursor=None):
        for p in self._load():
            if p.id == post.id:
                return Post.from_dict(p.to_dict()), None
        return post, None

    def comments(self, post, cursor=None):
        for p in self._load():
            if p.id == post.id:
                return [Comment.from_dict(c.to_dict()) for c in p.comments], None
        return [], None

    def image_base(self):
        return os.path.join(self.root, "img")


def build_sources(demo_root, cache_root=None, config=None, log=None):
    """登记所有来源：**混流打头**（默认落在它上面），然后是各真实来源，最后是离线示例。

    真实来源住在 feed_sources.py：那个文件（或它依赖的 requests）出问题时**只退回示例源**，
    社区页照样打得开——一个站改接口不该把整页弄瘫。"""
    sources = [DemoSource(demo_root)]
    if cache_root is None:
        cache_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "feed")
    try:
        import feed_sources
        real = feed_sources.build_real_sources(cache_root=cache_root, config=config, log=log)
        if real:
            sources = list(real) + sources
    except Exception as e:
        if log:
            try:
                log("社区真实来源加载失败，只用离线示例: %s" % e)
            except Exception:
                pass
    return sources


# --------------------------------------------------------------------------- 导航（纯逻辑）

def source_comments_page(src, post, cursor=None):
    """统一入口：来源有 `comments_page` 就用它；没有（鸭子类型的替身/老适配器）退回 `comments()`。

    探针里的替身来源就是普通对象（不继承 `SourceAdapter`），详情取数如果直接点
    `src.comments_page` 会 AttributeError，整页正文都取不回来。
    """
    fn = getattr(src, "comments_page", None)
    if fn is not None:
        return fn(post, cursor)
    try:
        items, why = src.comments(post, cursor)
    except TypeError:
        items, why = src.comments(post)
    return items, why, None


class FeedNav(object):
    """社区页状态机：列表 → 详情 → 返回，外加评论上的上下移动与折叠。

    刻意不碰任何界面对象：探针可以把它当纯逻辑测，main.py 只在它变化后重画。
    取数在后台线程里跑，"该重新取数了"用 stale 表达，什么时候换回来由 main 决定。"""

    def __init__(self):
        self.view = "list"          # list | detail
        self.posts = []
        self.index = 0
        self.post = None            # 详情页的帖子
        self.roots = []             # 详情页的评论树（原始嵌套结构）
        self.flat = []              # 拍平后的 [(comment, depth, parent)]
        self.c_index = 0
        self.collapsed = set()      # 折叠的评论 id
        self.cursor = None          # 列表分页游标
        self.more = True            # 还有没有下一页
        self.channel = "全部"
        self.stale = True           # 需要（重新）取数
        self.detail_loaded = False  # 详情正文/评论是否已经取回来
        self.c_cursor = None        # 评论分页游标
        self.c_more = False         # 评论还有没有下一页
        self.c_show = COMMENT_PAGE  # 屏幕上摆几条顶层评论
        self.child_show = {}        # 每条评论已经摊开几条回复
        self.child_saved = {}       # 折起来之前摊开几条（X 摊开时回到原值，不是回到 3）
        # 详情里还能再进一层时用它回退（知乎：某个回答 → 这个问题的全部回答）。
        # 退格先退栈，栈空了才回列表。
        self.stack = []
        self.stack_via = []     # 与 stack 同步：每层是"怎么进来的"

    # --- 列表 ---
    def reset(self, channel=None):
        self.view = "list"
        self.posts = []
        self.index = 0
        self.post = None
        self.roots = []
        self.flat = []
        self.c_index = 0
        self.collapsed = set()
        self.cursor = None
        self.more = True
        self.stale = True
        self.detail_loaded = False
        self.c_cursor = None        # 评论分页游标
        self.c_more = False         # 评论还有没有下一页
        self.c_show = COMMENT_PAGE  # 屏幕上摆几条顶层评论（其余的按"继续加载"摊开）
        self.child_show = {}        # 每条评论已经摊开几条回复
        self.child_saved = {}       # 折起来之前摊开几条（X 摊开时回到原值，不是回到 3）
        self.stack = []
        self.stack_via = []     # 与 stack 同步：每层是"怎么进来的"
        if channel is not None:
            self.channel = channel

    def add_page(self, posts, cursor):
        self.posts.extend(posts or [])
        self.cursor = cursor
        self.more = cursor is not None
        self.stale = False
        if self.index >= len(self.posts):
            self.index = max(0, len(self.posts) - 1)

    def selected(self):
        if 0 <= self.index < len(self.posts):
            return self.posts[self.index]
        return None

    def move(self, step):
        if not self.posts:
            return False
        new = max(0, min(len(self.posts) - 1, self.index + step))
        if new == self.index:
            return False
        self.index = new
        return True

    def near_end(self):
        """快到底了就该续下一页（选中项靠近末尾时触发）。"""
        return bool(self.more and self.posts and self.index >= len(self.posts) - 2)

    # --- 详情 ---
    def open_selected(self):
        p = self.selected()
        if p is None:
            return False
        self.view = "detail"
        self.post = p
        self.roots = []
        self.flat = []
        self.c_index = 0
        self.collapsed = set()
        self.detail_loaded = False
        self.c_cursor = None        # 评论分页游标
        self.c_more = False         # 评论还有没有下一页
        self.c_show = COMMENT_PAGE  # 屏幕上摆几条顶层评论（其余的按"继续加载"摊开）
        self.child_show = {}        # 每条评论已经摊开几条回复
        self.child_saved = {}       # 折起来之前摊开几条（X 摊开时回到原值，不是回到 3）
        return True

    def open_detail(self, post, via=""):
        """详情里再进一层（知乎：回答 → 它所属问题的全部回答）。

        当前这一层整份压进 `stack`，退格原样回来——不用重新取数，也不丢折叠状态。
        """
        if post is None:
            return False
        if self.view == "detail" and self.post is not None:
            self.stack.append((self.post, self.roots, self.flat,
                               self.c_index, set(self.collapsed)))
            self.stack_via.append(via or "")
        self.view = "detail"
        self.post = post
        self.roots = []
        self.flat = []
        self.c_index = 0
        self.collapsed = set()
        self.detail_loaded = False
        self.c_cursor = None        # 评论分页游标
        self.c_more = False         # 评论还有没有下一页
        self.c_show = COMMENT_PAGE  # 屏幕上摆几条顶层评论（其余的按"继续加载"摊开）
        self.child_show = {}        # 每条评论已经摊开几条回复
        self.child_saved = {}       # 折起来之前摊开几条（X 摊开时回到原值，不是回到 3）
        return True

    def set_detail(self, post, comments, cursor=None):
        self.post = post
        self.roots = list(comments or [])
        self.c_cursor = cursor
        self.c_more = cursor is not None
        self.flat = comment_forest(self.roots)
        self.detail_loaded = True
        if self.c_index >= len(self.visible_comments()):
            self.c_index = 0

    def _node_by_id(self, cid, nodes=None):
        for c in (self.roots if nodes is None else nodes):
            if c.id == cid:
                return c
            hit = self._node_by_id(cid, c.replies)
            if hit is not None:
                return hit
        return None

    def add_comments(self, comments, cursor):
        """又取回来一页评论：接在后面（已经有的按 id 去重，翻页重叠不会重一遍）。"""
        have = set(c.id for c in self.roots)
        new = [c for c in (comments or []) if c.id not in have]
        self.roots.extend(new)
        self.flat = comment_forest(self.roots)
        self.c_cursor = cursor
        self.c_more = bool(cursor is not None and new)   # 这一页一条新的都没有 = 到头了
        self.c_show = max(self.c_show, len(self.roots))     # 新一页到了直接摆出来
        return len(new)

    def child_limit(self, c):
        return self.child_show.get(c.id, CHILD_SHOW)

    def hidden_children(self, c):
        return max(0, len(c.replies) - self.child_limit(c))

    def has_more_comments(self):
        """还有没摆出来的（已经取回来的 + 来源说还有下一页的）。"""
        return bool(self.c_more or len(self.roots) > self.c_show)

    def show_more_comments(self):
        """把已经取回来的下一批顶层评论摆出来；返回这次多摆了几条（0 = 手里没有了）。"""
        if len(self.roots) <= self.c_show:
            return 0
        before = self.c_show
        self.c_show = min(len(self.roots), self.c_show + COMMENT_PAGE)
        return self.c_show - before

    def add_children(self, cid, children, total=0):
        """来源又取回来几条子评论：接到这条底下（按 id 去重），新回来的直接摆出来。

        小红书一页只给 1 条预览，剩下的得单独问——所以光靠 `child_show` 摊开手里那一条
        是不够的，树本身要能长。"""
        node = self.comment_by_id(cid)
        if node is None:
            return 0
        have = set(x.id for x in node.replies)
        new = [c for c in (children or []) if c.id not in have]
        if not new:
            return 0
        node.replies.extend(new)
        if total:
            node.n_replies = max(int(total), len(node.replies))
        self.child_show[str(cid)] = len(node.replies)   # 新取回来的一次全摆出来
        self.child_saved.pop(str(cid), None)            # 摊开了就不是折着的
        self.flat = comment_forest(self.roots)
        return len(new)

    def comment_by_id(self, cid):
        """按 id 找一条评论（顶层或任意深）。"""
        return self._node_by_id(str(cid))

    def show_more_children(self, cid):
        """把这条评论**已经取回来**的下一批回复摆出来；返回多摆了几条。"""
        node = self._node_by_id(cid)
        if node is None:
            return 0
        lim = self.child_limit(node)
        if lim >= len(node.replies):
            return 0
        new = min(len(node.replies), lim + CHILD_SHOW)
        got = new - lim
        self.child_show[cid] = new
        self.child_saved.pop(str(cid), None)     # 手动摊开过了，旧记忆作废
        return got

    def thread_root(self, c):
        """这条评论所属那一楼的楼根（顶层评论）。子评论上按 X 也折整楼。"""
        if c is not None and getattr(c, "more_for", ""):
            # "还有 N 条回复"那一行本身不是评论：它代表的楼就是它挂着的那条
            node = self.comment_by_id(c.more_for)
            if node is not None:
                c = node
        node, seen = c, 0
        while node is not None and seen < 20:
            parent = self.parent_of(node)
            if parent is None or parent is node:
                return node
            node, seen = parent, seen + 1
        return node or c

    def folded(self, c=None):
        """这一楼是不是折着的（回复全收、只留楼根和"还有 N 条回复"那一行）。"""
        root = self.thread_root(c if c is not None else self.current_comment())
        if root is None:
            return False
        # 只认**当前**摆了几条，不认 `child_saved` 里那份"折之前的记忆"：
        # 折起来之后点「还有 N 条回复」摊开 3 条，这一楼其实已经摊开了，
        # 再按 X 该是"折"，不能因为记忆表里还有条目就反过来"摊"（真人报的冲突）。
        return self.child_limit(root) <= 0

    def fold_thread(self, c=None):
        """把这一楼下面的回复折起来（评论本身照读）；折成功返回 1。

        实现就是 `child_show[root] = 0`：`visible_comments` 里"没摆出来的"那一段会自动
        变成「还有 N 条回复 · 点开」，点/回车摊开 3 条——和分页同一句话、同一套动作，
        所以界面上不多一个词。折之前摊开几条记进 `child_saved`，摊开时回到原值。
        """
        root = self.thread_root(c if c is not None else self.current_comment())
        if root is None or not root.replies:
            return 0                       # 没有子回复：折不动，就当没这个动作
        lim = self.child_limit(root)
        if lim <= 0:
            return 0                       # 本来就折着
        self.child_saved[root.id] = lim
        self.child_show[root.id] = 0
        self._cursor_to(root)
        return 1

    def unfold_thread(self, c=None):
        """把这一楼摊回折之前的样子；摊开成功返回 1。"""
        root = self.thread_root(c if c is not None else self.current_comment())
        if root is None or root.id not in self.child_saved:
            return 0
        lim = int(self.child_saved.pop(root.id) or CHILD_SHOW)
        self.child_show[root.id] = max(CHILD_SHOW, lim)
        self._cursor_to(root)
        return 1

    def _cursor_to(self, node):
        """光标挪到这条上（它要是被折没了就退到还能看见的那一条）。"""
        if node is None:
            return
        ids = [c.id for c, _d, _p in self.visible_comments()]
        if node.id in ids:
            self.c_index = ids.index(node.id)
        else:
            self._clamp_comment_index()

    def fold_all(self):
        """整页只看主楼：每一楼的回复都折起来；返回折了几楼（0 = 本来就都折着）。"""
        n = 0
        for root in self.roots:
            if not root.replies:
                continue
            lim = self.child_limit(root)
            if lim <= 0:
                continue
            self.child_saved[root.id] = lim
            self.child_show[root.id] = 0
            n += 1
        if n:
            self._clamp_comment_index()
        return n

    def unfold_all(self):
        """把整页折起来的楼全摊回去（各自回到折之前摊开的条数）。"""
        n = 0
        for cid in list(self.child_saved):
            node = self.comment_by_id(cid)
            lim = int(self.child_saved.pop(cid) or CHILD_SHOW)
            if node is None:
                continue                   # 这条已经不在这棵树里了，记忆一起丢掉
            self.child_show[cid] = max(CHILD_SHOW, lim)
            n += 1
        if n:
            self._clamp_comment_index()
        return n

    def back(self):
        if self.view == "list":
            return False
        if self.stack:
            # 栈里还有一层：回上一层详情，内容原样（不重新取数）
            self.post, self.roots, self.flat, self.c_index, self.collapsed = self.stack.pop()
            if self.stack_via:
                self.stack_via.pop()
            self.view = "detail"
            self.detail_loaded = True
            return True
        self.view = "list"
        self.post = None
        self.roots = []
        self.flat = []
        self.c_index = 0
        self.collapsed = set()
        self.detail_loaded = False
        self.c_cursor = None        # 评论分页游标
        self.c_more = False         # 评论还有没有下一页
        self.c_show = COMMENT_PAGE  # 屏幕上摆几条顶层评论（其余的按"继续加载"摊开）
        self.child_show = {}        # 每条评论已经摊开几条回复
        self.child_saved = {}       # 折起来之前摊开几条（X 摊开时回到原值，不是回到 3）
        self.stack = []
        self.stack_via = []     # 与 stack 同步：每层是"怎么进来的"
        return True

    def home(self):
        """**一次退到列表主页**：详情里不管压了几层（知乎：回答 → 这个问题）都退到底。

        真人要的是"在回答里点标题进了问题页，Esc/Z 就直接回主页"，不是先退回那个回答。
        """
        if self.view == "list":
            return False
        while self.back():
            pass
        return self.view == "list"

    def move_comment(self, step):
        """上下移动光标——**在"看得见的评论"里移动**，不是在整个拍平序列里。

        `c_index` 一律指 `visible_comments()`（屏幕上那串）的下标：折叠之后被藏起来的
        子树不该还能被选中（原来按 `flat` 取下标，折叠后往下按一格就会停到一条看不见的
        评论上，高亮跟着消失——"高亮不见了"那类怪毛病就是这么来的）。"""
        vis = self.visible_comments()
        if not vis:
            return False
        new = max(0, min(len(vis) - 1, self.c_index + step))
        if new == self.c_index:
            return False
        self.c_index = new
        return True

    def current_comment(self):
        vis = self.visible_comments()
        if 0 <= self.c_index < len(vis):
            return vis[self.c_index][0]
        return None

    def toggle_collapse(self):
        """折叠/展开当前选中那条。

        以前只有"带回复"的才折叠得动——真人说得很清楚：点上去只想把这一条收起来，
        长评论也是一条，凭什么不给收。所以现在**任何一条**都能收：
        收起 = 只留作者行 + 一行摘要（子回复一起藏起来）。
        """
        cur = self.current_comment()
        if cur is None:
            return False
        if cur.id in self.collapsed:
            self.collapsed.discard(cur.id)
        else:
            self.collapsed.add(cur.id)
        self._clamp_comment_index()
        return True

    def _clamp_comment_index(self):
        vis = self.visible_comments()
        if not vis:
            self.c_index = 0
            return
        cur = self.current_comment()
        ids = [c.id for c, _d, _p in vis]
        if cur is not None and cur.id in ids:
            self.c_index = ids.index(cur.id)
        else:
            self.c_index = max(0, min(len(vis) - 1, self.c_index))

    def visible_comments(self):
        """按折叠状态过滤后的显示序列（**只含屏幕上真摆出来的那些**）。

        折叠：被折叠节点的整个子树都不显示（一个实现，递归到底）。
        分页：顶层只走到 `c_show`、每条只走到 `child_show[cid]`；后面还有的用一行
        "还有 N 条回复 · 回车展开"占位，顶层剩下的在整页末尾写"还有 N 条评论"。

        以前这里把整棵树一次全给出去，一页几百条评论直接铺到底——真人原话
        "支持继续加载，不论是帖子还是评论还是子评论"。
        """
        out = []

        def walk(nodes, depth, parent=None):
            for c in nodes:
                out.append((c, depth, parent))     # 父节点留着：表头要写"回复 X"
                if c.id in self.collapsed:
                    continue
                lim = self.child_limit(c)
                walk(list(c.replies[:lim]), depth + 1, c)
                # N 要算**站上真实还剩多少**：小红书首页只给 1 条预览，`n_replies` 是它给的
                # 总数（33 之类），本地拿到的比它多就按本地算（别的来源不填这个字段）。
                total = max(len(c.replies), int(getattr(c, "n_replies", 0) or 0))
                rest = total - min(lim, len(c.replies))
                if rest > 0:
                    out.append((Comment(
                        cid="__more__" + c.id,
                        body=[Block(BLOCK_P, "还有 %d 条回复 · 点开" % rest)],
                        more_for=c.id), depth + 1, c))

        walk(list(self.roots[:max(1, int(self.c_show or COMMENT_PAGE))]), 0)
        return out

    def parent_of(self, c):
        """这条评论挂在哪条下面（顶层返回 None）。"""
        for node, _d, p in (self.flat or []):
            if node is c:
                return p
        return None

    def reply_to_of(self, c):
        """这条评论在回谁 —— 表头里那个"回复 X"。

        **所有来源统一在这里算**，渲染层只管显示：
        1. 树里有父节点（IT之家/知乎/Discourse 这些真层级）：父节点的作者；
        2. 平铺来源（虎扑/NGA/V2EX）把引用写进正文开头（`@张三：…` 或 `回复 @张三：…`）：
           把那个名字提出来，正文里那截前缀同时省掉（不然"回复 张三"和"@张三："重复）。
        自己回自己、父作者为空、正文开头已经写了同一个人，都不写。
        """
        if c is None:
            return ""
        me = (c.author or "").strip()
        # 来源自己给了收件人（虎扑 quote 里有作者名）：最可信，直接用
        who = (getattr(c, "reply_to", "") or "").strip()
        if who and who != me:
            return who
        # 其次看正文开头点名的人：楼层是平铺的（虎扑/NGA/V2EX）或者回复的不是直系父节点
        # （Discourse 可以回到很上面那一层），@ 出来的名字才是真的收件人。
        who = _mention_target(c)
        if who and who != me:
            return who
        parent = self.parent_of(c)
        if parent is not None and parent is not c:
            who = (parent.author or "").strip()
            if who and who != me and not _opens_with_mention(c, who):
                return who
        return ""

    def comment_roots(self):
        return self.roots

    def comment_stats(self):
        total = sum(c.total() for c in self.roots)
        return total, len(self.visible_comments()), len(self.collapsed)


# --------------------------------------------------------------------------- 渲染

def esc(text):
    return html.escape(str(text or ""), quote=False)


def _is_number(cell):
    """这一格看着像数字（右对齐用）。"10-12"、"43.0%"、"+8" 都算。"""
    return bool(re.match(r"^[+-]?\d[\d.:%+\-/]*$", (cell or "").strip()))


def _table_html(text, theme, f, nowrap=False):
    """TSV → 表格。第一行是表头（弱化 + 加粗），数字列右对齐。

    为什么要真表格：战绩、技术统计、一行行回合，本来就是"列对齐了才读得下去"的东西；
    用 ` · ` 串成一句话，一屏下来糊成一片（真人原话："排版太丑，很难看"）。
    Qt 的富文本只认一部分 CSS：`cellspacing/cellpadding/width` 和 `td` 上的
    `align/font-*/color` 是稳的，`border-collapse`、每格边框、半透明底色都不稳，
    所以这里不画线，靠留白和列对齐分层。

    `nowrap=True`（表格块带 `lang="wide"`）：单元格加 `white-space:nowrap`，Qt 让表格
    按**自然宽度**撑出文档宽，于是能靠左右滑看到右边的列（17 列的技术统计自然宽 ~920px）。
    注意 Qt 富文本的滚动条是**整页一个**，不是每张表一个：所以横滑时整页一起动，
    并且横条**不显示**（真人要的："左右滑不要显示进度条"，见 `main._feed_hslide`）。
    """
    rows = []
    for line in (text or "").split("\n"):
        if line.strip(" \t"):
            rows.append(line.split("\t"))
    if not rows:
        return ""
    cells_font = max(11, int(f["sub"]))
    pad = max(2, int(round(cells_font * 0.22)))
    # 每一列的朝向由**数据**决定：整列都是数字就右对齐。表头跟着列走——
    # 表头右对齐、数据左对齐（或者反过来）最难看，看起来像没对齐的表格。
    cols = max(len(r) for r in rows)
    aligns = []
    for j in range(cols):
        vals = [(r[j].strip() if j < len(r) else "") for r in rows[1:]]
        vals = [v for v in vals if v]
        numeric = bool(vals) and all(_is_number(v) for v in vals)
        aligns.append("right" if numeric and j > 0 else "left")
    parts = ['<table width="100%" cellspacing="0" cellpadding="' + str(pad)
             + '" style="margin:10px 0 2px 0;">']
    for i, cells in enumerate(rows):
        parts.append("<tr>")
        for j, raw in enumerate(cells):
            cell = (raw or "").strip()
            text_html = html.escape(cell) if cell else "&nbsp;"
            nowrap_css = "white-space:nowrap;" if nowrap else ""
            if i == 0:
                # 表头：比正文弱一档，加粗，别抢正文的注意力
                style = ('font-size:%dpx; color:%s; font-weight:bold;' %
                         (max(10, cells_font - 1), theme["faint"])) + nowrap_css
            else:
                color = theme["text"] if j else theme["mark"]
                style = ('font-size:%dpx; color:%s;' % (cells_font, color)) + nowrap_css
            align = aligns[j] if j < len(aligns) else "left"
            parts.append('<td align="%s" style="%s">%s</td>' % (align, style, text_html))
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _nl(text):
    return esc(text).replace("\n", "<br>")


FEED_FONT_STACK = ('"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", '
                   '"Segoe UI", "Noto Sans CJK SC", sans-serif')
FEED_SIZE_DEFAULT = 15          # 社区页自己的默认字号：一屏能放下七八条
TITLE_CLIP = 62                 # 标题最多这么多字，再多截断（见 _clip）
EXCERPT_CLIP = 78


def _font_css(family):
    """把字体名拼成能塞进 style 的一串。

    社区页默认用**界面无衬线字体栈**，不跟书页走：书页是长文，字号大、常常是宋体；
    同一套字号搬到列表上就是"每屏两条巨字"。真人说过"帖子太难看"，一半来自这儿。

    引号一律换成单引号：双引号写在 HTML 属性里会被转义成 `&quot;`，Qt 解析 font-family
    时会当场断掉，于是又退回控件字体（实测就是被 Courier New 带跑的那次）。
    """
    fam = str(family or "").replace('"', "'").replace(";", " ").replace("<", " ")
    return fam.strip() or FEED_FONT_STACK


def _clip(text, limit):
    """压成一行、超长截断。

    截断只发生在**渲染**这一侧，所以 `post_needle()` 用的是同一个 `_clip`——否则界面层
    拿完整标题去文档里找，永远找不着，高亮和"点第几条"一起失效。
    """
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[:limit].rstrip("，。、；：,.;:!?！？ ") + "…"


def _scale(theme):
    """字号阶梯：全部由社区页自己那个字号推出来，一份数字管全文。

    标题**只比正文大 1px**（真人两轮反馈："标题太大太显眼"）——靠加粗和亮一档的强调色
    分层就够了，字号别再往上顶；一屏里标题比正文还抢眼的话，读起来像公告栏不像信息流。
    """
    s = int(theme["size"])
    return {
        "title": s + 1,
        "head": s,
        "body": s,
        "sub": max(11, s - 1),
        "meta": max(10, int(round(s * 0.72))),
    }


def theme_from_config(cfg, width_px=420, text_color=None):
    """把阅读器的排版设置折成渲染主题。**社区页有自己的字号和字体**（`feed_font_size` /
    `feed_font_family`），不再拿书页那套顶上来——没配就用界面字体栈 + 15px。

    颜色一律从**正文此刻实际用的颜色**派生（`text_color` 由界面层传进来：auto/幽灵模式会把
    正文换成纯黑或纯白，透明窗口下这个值不能靠 config 猜）。派生规则只有一条：

        往中等灰靠近 = 弱化，离开中等灰 = 强调

    而且只在**亮度**上推，不在 RGB 上乘系数——原来 `216 * 0.45 = 97`（深灰），压在透明/
    半透明窗口上就是看不见（真人反馈的"字体颜色导致看不清"）。同一个"弱化"改成亮度上推：
    216 → 176，仍然清楚，只是不抢眼。不管正文是深色还是浅色，弱化都朝中间、强调都朝两端，
    两边的可读性都不塌。
    """
    p = _parse_color(text_color if text_color is not None else cfg.get("text_color"))
    try:
        size = int(cfg.get("feed_font_size") or 0)
    except Exception:
        size = 0
    if not 11 <= size <= 22:                   # 没配、或者配了个不可能的数值
        size = FEED_SIZE_DEFAULT
    return {
        "text": _rgba(p),
        "mark": _rgba(_toward_mid(p, -0.34)),      # 标题/强调：比正文更抢眼（浅色正文→更白）
        "dim": _rgba(_toward_mid(p, 0.24)),        # 次要信息（摘要）
        "faint": _rgba(_toward_mid(p, 0.42)),      # 更次要（时间、统计、来源）
        "accent": _rgba(_accent(p)),               # 来源名：色相偏蓝，亮度不动
        "size": size,
        "family": _font_css(cfg.get("feed_font_family")),
        "lowkey": bool(cfg.get("feed_lowkey", False)),
        "width": max(240, int(width_px)),
    }


def _parse_color(value, default=(216, 216, 216, 255)):
    """吃下 config 里实际会出现的几种写法：#rgb / #rrggbb / rgb() / rgba()。

    原来这里只认 `#rrggbb`，而阅读器默认写的就是 `rgba(200, 200, 200, 255)`——解析失败后
    静默回落到硬编码灰，于是"跟着阅读设置走"是假的，连真人自己调过的字色都不生效。
    """
    s = str(value or "").strip()
    try:
        if s.startswith("#"):
            h = s[1:]
            if len(h) == 3:
                h = "".join(ch * 2 for ch in h)
            if len(h) >= 6:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        m = re.match(r"rgba?\(([^)]*)\)", s, re.I)
        if m:
            nums = [x.strip() for x in m.group(1).split(",")]
            r, g, b = (int(round(float(nums[i]))) for i in range(3))
            a = int(round(float(nums[3]))) if len(nums) > 3 else 255
            clamp = lambda v: max(0, min(255, v))       # noqa: E731
            return (clamp(r), clamp(g), clamp(b), clamp(a))
    except Exception:
        pass
    return default


def _rgba(c):
    return "rgba(%d, %d, %d, %d)" % (c[0], c[1], c[2], c[3])


def _toward_mid(c, k):
    """按亮度把颜色朝中等灰推进（k>0）或推离（k<0）；色相、饱和度、透明度都不动。"""
    r, g, b = c[0] / 255.0, c[1] / 255.0, c[2] / 255.0
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l + (0.5 - l) * k))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)), c[3])


def _accent(c):
    """来源名/链接色：色相拉到偏蓝，**亮度保持原样**（亮度一动，可读性就跟着动）。"""
    r, g, b = c[0] / 255.0, c[1] / 255.0, c[2] / 255.0
    _h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(l, 0.64) if l >= 0.5 else min(l, 0.36)     # 别停在中等灰附近，那是最难认的一段
    r, g, b = colorsys.hls_to_rgb(210 / 360.0, l, max(0.35, min(0.7, s or 0.45)))
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)), c[3])


def _num(n):
    """统计数字的中文口语写法：13270000 -> "1327 万"、23500 -> "2.4 万"。

    知乎的热度动辄上千万，原样打在列表里既占地方又读不出来；这里只做一件事：
    超过一万就换算成"万 / 亿"，其余原样。"""
    n = int(n or 0)
    neg = n < 0
    n = abs(n)
    if n >= 100000000:
        s = ("%.1f" % (n / 100000000.0)).rstrip("0").rstrip(".") + " 亿"
    elif n >= 10000:
        s = ("%.1f" % (n / 10000.0)).rstrip("0").rstrip(".") + " 万"
    else:
        s = str(n)
    return ("-" + s) if neg else s


def _tag_text(tag):
    """标签可能是字符串，也可能是来源自己的字典（V2EX 的节点就是 `{id,name,slug}`）。

    以前直接把字典塞进 `esc()`，界面上会渲染出 `{'id': 26, 'name': 'emoji', ...}` 这种
    原样字典——真人一眼就能看出是程序漏出来的东西。
    """
    if isinstance(tag, dict):
        return str(tag.get("name") or tag.get("title") or tag.get("slug") or "").strip()
    return str(tag or "").strip()


def _meta_line(post, theme):
    """一行元信息：来源（强调色）· 作者 · 时间 · 赞 / 评论数，最多四段。

    以前赞和评论数单独压在卡片底部再打一行，两条帖子就是五六行小字，整屏全是灰点；
    合成一行以后一屏能多看一条。**超过四段就截断**：知乎和 IT之家会把标签、阅读数一起塞
    进来，不截的话这一行能比标题还长（实测那条有七段）。

    分隔符用中点、不用全角空格：Qt 的 HTML 解析会把 `　` 折成半角空格，最后挤成一团。
    """
    # 来源那一截只在真有标签时出现：赛程/比分这类页面自己有页签（文字直播/数据统计），
    # 再顶着"虎扑 · NBA"就是重复（真人 2026-10-22 把那三行圈出来说冗余）
    src = post.source_label
    stat = []
    if post.score:
        stat.append("%s %s" % (_num(post.score), post.score_label or "赞"))
    if post.n_comments:
        stat.append("%s %s" % (_num(post.n_comments), post.comment_label or "条评论"))
    tags = [x for x in (_tag_text(t) for t in list(post.tags or [])[:2]) if x]
    bits = [b for b in [post.author, post.created] + stat + tags if b]
    head = f'<span style="color:{theme["accent"]};">{esc(src)}</span>' if src else ""
    tail = " · ".join(esc(b) for b in bits[:4])
    return " · ".join([b for b in (head, tail) if b])


def _img_tag(src, theme, max_w):
    src = str(src or "")
    if "." not in src:
        # 低调模式下来源给的占位（不是文件名）：照样标出"这里有图"，但不放 <img>
        return f'<span style="color:{theme["faint"]};">[图]</span>'
    w = max(80, min(theme["width"] - 40, max_w))
    return f'<img src="{esc(src)}" width="{w}">'


def render_list(posts, nav, theme):
    """列表：一条一段，**只靠留白分层，不画分隔线**。

        标题          比正文大 1px、加粗、强调色，最多两行（超了截断）
        摘要          比正文小 1px、弱一档，两行以内
        来源 · 作者 · 时间 · 428 赞 · 6 条评论      一行小字，压到最弱
        [图]          缩到 240px 宽，不占满整屏

    真人两轮都提到这一列：先说"太难看"，再说"不要用横线隔开、标题太大太显眼"。
    所以分隔线全删了（细线在透明窗口上本来就偏显眼），改用**条与条之间 22px 的空白**；
    标题只比正文大 1px。**不用底色块**，也**不画"当前选中"标记**：HTML 是上一次渲染的
    产物，光标一动标记就成了过期信息（高亮只由 extra selection 画）。
    """
    if not posts:
        return _wrap(theme, f'<p style="margin:24px 0 0 0; color:{theme["faint"]};'
                            f' font-size:{_scale(theme)["sub"]}px;">还没有内容</p>')
    f = _scale(theme)
    parts = []
    for i, p in enumerate(posts):
        gap = 22 if i else 0                 # 条与条之间用留白分开，不画线
        parts.append(f'<a name="post-{esc(p.id)}"></a>')
        parts.append(
            f'<p style="margin:{gap}px 0 0 0; color:{theme["mark"]};'
            f' font-size:{f["title"]}px;'
            f' font-weight:bold; line-height:135%;">{esc(_clip(p.title, TITLE_CLIP))}</p>'
        )
        if p.excerpt:
            parts.append(
                f'<p style="margin:4px 0 0 0; color:{theme["dim"]};'
                f' font-size:{f["sub"]}px; line-height:150%;">'
                f'{esc(_clip(p.excerpt, EXCERPT_CLIP))}</p>'
            )
        meta = _meta_line(p, theme)
        if meta:
            parts.append(
                f'<p style="margin:6px 0 0 0; color:{theme["faint"]};'
                f' font-size:{f["meta"]}px;">{meta}</p>'
            )
        if p.cover:
            if theme["lowkey"]:
                parts.append(f'<p style="margin:6px 0 0 0; color:{theme["faint"]};'
                             f' font-size:{f["meta"]}px;">[图]</p>')
            else:
                parts.append(f'<p style="margin:8px 0 0 0;">'
                             f'{_img_tag(p.cover, theme, 240)}</p>')
    return _wrap(theme, "".join(parts))


def render_detail(post, nav, theme):
    """帖子详情：标题 / 元信息 / 正文 / 评论区，全用留白分层，不画横线。

    评论区和正文之间只留一行"12 条评论"，**不往正文里插任何说明句**——
    真人明确说过前端不要解释性文字（以前会写"评论在下面（10 条）。"这种东西）。
    """
    if post is None:
        return _wrap(theme, "")
    f = _scale(theme)
    title_style = (f'margin:0; color:{theme["mark"]}; font-size:{f["title"]}px;'
                   f' font-weight:bold; line-height:140%;')
    if theme.get("title_link"):
        # 这一页是从某个回答进来的：标题就是那个问题，点它能看这个问题的全部回答。
        # **不下划线**（真人 2026-10-19："知乎标题不用下划线来表示可点击"）：它本来就是
        # 强调色、也比正文粗，够明显了；加下划线反而像链接标记，读起来脏。
        pass
    parts = [
        f'<p style="{title_style}">{esc(post.title)}</p>',
        f'<p style="margin:6px 0 0 0; color:{theme["faint"]};'
        f' font-size:{f["meta"]}px;">{_meta_line(post, theme)}</p>',
    ]
    parts.extend(_block_paragraphs(post.body, theme))
    total, _shown, nfold = nav.comment_stats() if nav.detail_loaded else (0, 0, 0)
    if total:
        # 知乎一个问题底下列的是**回答**，不是评论（真人点名了这三层的名字）；
        # 其它来源都叫评论。数字用总条数（含子回复），表头只说这一层是什么。
        label = "回答" if (post.source == "zhihu" and str(post.id).isdigit()) else "评论"
        # 「回答」这一层数的是**几条回答**（根节点），不是把每条回答底下的评论也算进去；
        # 评论那一栏照旧数总数（含子回复），那才是"这帖下有多少条话"。
        n_head = len(nav.roots) if (label == "回答" and nav.detail_loaded) else total
        head = "%s 条%s" % (_num(n_head), label)
        if nfold:
            head += " · %d 处折叠" % nfold
        parts.append(
            f'<p style="margin:26px 0 0 0; color:{theme["faint"]};'
            f' font-size:{f["meta"]}px;">{esc(head)}</p>'
        )
        parts.append(render_comment_tree(nav, theme))
    return _wrap(theme, "".join(parts))


def _comment_header(c, reply_to=""):
    """评论表头那一行的正文：作者 · 回复 X · 时间 · 赞。

    渲染和"定位选中项"用的是同一份拼装——两处各拼一遍必然漂移（探针就抓到过一次：
    渲染用全角空格、定位用半角，结果高亮永远落不上）。折叠角是状态，由调用方追加。

    `reply_to` 由 `FeedNav.reply_to_of()` 算（各来源统一），这里只负责摆位置：
    紧跟在作者后面——"张三 回复 李四 · 3 赞 · 2 小时前"，一眼看出来在回谁。"""
    bits = [c.author or "匿名"]
    if reply_to:
        bits.append("回复 " + reply_to)
    if c.created:
        bits.append(c.created)
    if c.score:
        bits.append(f"{c.score} 赞")
    return " · ".join(bits)



COMMENT_PAGE = 20          # 详情页一次摆几条顶层评论（剩下的"继续加载"）
CHILD_SHOW = 3             # 每条评论先摆几条回复（剩下的"还有 N 条回复 · 回车展开"）
COMMENT_PREVIEW = 54       # 折叠时留一行摘要的字数：知道这条在说什么就够了
COMMENT_INDENT = 16        # 每层缩进
COMMENT_MAX_DEPTH = 6      # 缩进封顶：再深就挤成一列字了（reddit 也是到一定层数就不再缩）


def render_comment_tree(nav, theme):
    """树状评论，照 reddit 那套排：**每条一个作者行，回复往里缩一层**。

    以前是把子回复折成一行 `「李四：同意」` 塞进父评论正文里——看着像引用，其实是回复，
    层级全丢了，真人原话"评论和子评论没处理好，应该像 reddit 那样"。

    现在：
    - 每条评论 = 作者行（作者 · 回复 X · 时间 · 赞，最小号、最弱色）+ 正文（正文号、正常色）；
    - 子回复作为独立一条，左边距多 16px，层级一眼看出来；缩进封顶 6 层，再深就不再往里缩；
    - 作者行前面放 `▾`（展开）/ `▸`（收起，有子回复时写 `▸ n`）；点这条或按 `Z` 切换；
    - **收起 = 这条也收起来**：只留作者行 + 一行摘要（`COMMENT_PREVIEW` 字）；
    - 一页不全铺：顶层先摆 `COMMENT_PAGE` 条、每条先摆 `CHILD_SHOW` 条回复，
      剩下的用"还有 N 条……"占位（滚到底/回车继续加载，见 FeedNav.show_more_*）；
    - 表头文本本身不动，`comment_needle()` 拿来定位照样命中（它匹配的是表头那段）。
    """
    vis = nav.visible_comments()
    if not vis:
        return ""
    f = _scale(theme)
    parts = []
    for c, depth, _p in vis:
        depth = max(0, int(depth))
        ind = COMMENT_INDENT * min(depth, COMMENT_MAX_DEPTH)
        gap = 14 if depth == 0 else 8
        if getattr(c, "more_for", ""):
            # "还有 N 条回复"：一行弱色小字，回车摊开（数据就在手边，不再发请求）
            parts.append(
                f'<p style="margin:6px 0 0 {ind}px; color:{theme["faint"]};'
                f' font-size:{f["meta"]}px;">{esc(_comment_text(c))}</p>'
            )
            continue
        reply_to = nav.reply_to_of(c) if nav is not None else ""
        head = _comment_header(c, reply_to)
        folded = c.id in nav.collapsed
        if folded:
            n_hidden = len(descendant_ids(c))
            head = ("▸ %d · %s" % (n_hidden, head)) if n_hidden else ("▸ " + head)
        elif c.replies:
            # 只有"下面还挂着东西"的才标 ▾：没有回复的那条展开时就是干净一行，
            # 收起来之后才出现 ▸（不然每条前面都挂个 ▾，像一堆没用的按钮）
            head = "▾ " + head
        parts.append(
            f'<p style="margin:{gap}px 0 0 {ind}px; color:{theme["faint"]};'
            f' font-size:{f["meta"]}px;">{esc(head)}</p>'
        )
        if folded:
            preview = _clip(_comment_text(c, reply_to), COMMENT_PREVIEW)
            if preview:
                parts.append(
                    f'<p style="margin:2px 0 0 {ind}px; color:{theme["dim"]};'
                    f' font-size:{f["sub"]}px; line-height:150%;">{esc(preview)}</p>'
                )
            continue
        for b in _without_mention(c.body or [], reply_to):
            parts.append(
                f'<p style="margin:2px 0 0 {ind}px; color:{theme["text"]};'
                f' font-size:{f["sub"]}px; line-height:160%;">'
                f'{_block_inline(b, theme)}</p>'
            )
    # 顶层还没摆出来的：写一行占位，滚到底自动接着加载（见 main.feed_autoload_more）
    hidden = max(0, len(nav.roots) - max(1, int(nav.c_show or COMMENT_PAGE)))
    if hidden or nav.c_more:
        tail = ("还有 %d 条评论" % hidden) if hidden else "还有更多评论"
        parts.append(
            f'<p style="margin:10px 0 0 0; color:{theme["faint"]};'
            f' font-size:{f["meta"]}px;">{esc(tail)}</p>'
        )
    return "".join(parts)


def _comment_text(c, reply_to=""):
    """一条评论的纯文本（折叠摘要用）：去掉图块，段落之间留一个空格。"""
    return " ".join(str(b.text or "").strip()
                    for b in _without_mention(c.body or [], reply_to)
                    if b.text and b.text.strip())


_MENTION_RE = re.compile(r"^\s*(?:回复\s*)?@\s*([^\s@：:，,、]{1,24})\s*[：:，,、\s]")


def _mention_target(c):
    """正文开头 `@某人：` / `回复 @某人：` 里的那个名字（平铺来源的"回复谁"）。"""
    for b in (c.body or []):
        text = str(b.text or "")
        if not text.strip():
            continue
        hit = _MENTION_RE.match(text)
        return hit.group(1) if hit else ""
    return ""


def _opens_with_mention(c, who):
    """正文开头已经点名了同一个人（知乎的子评论就是这样），表头就别再说一遍。"""
    for b in (c.body or []):
        text = str(b.text or "").lstrip()
        if not text:
            continue
        for form in ("@" + who, "回复 @" + who, "回复@" + who):
            if text.startswith(form):
                return True
        return False
    return False


def _strip_mention(text, who):
    """把开头那截 `@某人：` / `回复 @某人：` 削掉（表头已经写了"回复 某人"）。"""
    if not (who and text):
        return text
    pat = r"^\s*(?:回复\s*)?@\s*" + re.escape(who) + r"\s*[：:，,、]?\s*"
    new = re.sub(pat, "", text, count=1)
    return new if new.strip() else text


def _without_mention(blocks, who):
    """正文第一块开头那句 `@某人：` 去掉；别的块原样。"""
    if not who or not blocks:
        return blocks
    first = blocks[0]
    text = str(first.text or "")
    stripped = _strip_mention(text, who)
    if stripped == text:
        return blocks
    out = [Block(first.kind, stripped, first.url, first.lang)]
    out.extend(blocks[1:])
    return out


def _block_inline(b, theme):
    if b.kind == BLOCK_IMG:
        return (f'<span style="color:{theme["faint"]};">[图]</span>' if theme["lowkey"]
                else _img_tag(b.url or b.text, theme, 360))
    if b.kind == BLOCK_H:
        return f'<span style="font-weight:bold; color:{theme["mark"]};">{_nl(b.text)}</span>'
    if b.kind == BLOCK_QUOTE:
        return f'<span style="color:{theme["dim"]};">{_nl(b.text)}</span>'
    if b.kind == BLOCK_CODE:
        return (f'<span style="font-family:Consolas,\'Courier New\',monospace;'
                f' color:{theme["dim"]};">{_nl(b.text)}</span>')
    if b.kind == BLOCK_LIST:
        return f'<span>· {_nl(b.text)}</span>'
    return _nl(b.text)


def _block_paragraphs(blocks, theme):
    """正文块渲染成独立段落（每块一个 <p>，有自己的间距和行高）。

    不用 <br> 把整篇串成一整段：那样段间距全丢，一屏密密麻麻，和书页排版完全两回事。
    行高统一 175%——中文正文在 1.7 上下最好读，压到 1.4 就成"报纸广告"。
    """
    f = _scale(theme)
    out = []
    for b in blocks:
        if b.kind == BLOCK_IMG:
            if theme["lowkey"]:
                out.append(f'<p style="margin:11px 0 0 0; color:{theme["faint"]};'
                           f' font-size:{f["meta"]}px;">[图]</p>')
            else:
                out.append(f'<p style="margin:11px 0 0 0;">'
                           f'{_img_tag(b.url or b.text, theme, 460)}</p>')
            continue
        if b.kind == BLOCK_TABLE:
            # `lang` 在表格块上表示"这行要横着看"：`wide` = 列多，不许折行，让表格按
            # 自然宽度撑出去，靠左右滑看右边的列（横条不显示，见 main.feed_hscroll_*）。
            out.append(_table_html(b.text, theme, f, nowrap=(b.lang or "") == "wide"))
            continue
        if b.kind == BLOCK_H:
            style = (f'margin:17px 0 0 0; font-size:{f["head"]}px; line-height:150%;'
                     f' font-weight:bold; color:{theme["mark"]};')
        elif b.kind == BLOCK_QUOTE:
            style = (f'margin:10px 0 0 12px; font-size:{f["body"]}px; line-height:175%;'
                     f' color:{theme["dim"]};')
        elif b.kind == BLOCK_CODE:
            style = (f'margin:10px 0 0 0; font-size:{f["sub"]}px; line-height:150%;'
                     f' color:{theme["dim"]}; font-family:Consolas,monospace;')
        else:
            style = (f'margin:9px 0 0 0; font-size:{f["body"]}px; line-height:175%;'
                     f' color:{theme["text"]};')
        text = ("· " + b.text) if b.kind == BLOCK_LIST else b.text
        out.append(f'<p style="{style}">{_nl(text)}</p>')
    return out


def _wrap(theme, body):
    return (f'<div style="font-family:{theme["family"]}; color:{theme["text"]};'
            f' line-height:150%;">{body}</div>')


def status_text(nav, source_label=""):
    """页脚那一行**只放信息**，不放操作提示。

    真人明确要求过页脚"不用按钮、不用操作提示，只要必要的信息"——社区页沿用同一条，
    所以这里全是状态（第几条 / 评论多少 / 还有没有下一页），一个键位提示都不出现。"""
    if nav.view == "detail" and nav.post is not None:
        p = nav.post
        bits = [p.source_label or p.source]
        if p.channel:
            bits.append(p.channel)
        if nav.detail_loaded:
            total, _shown, nfold = nav.comment_stats()
            if total:
                bits.append(f'评论 {total} 条' + (f'（折叠 {nfold} 处）' if nfold else ""))
        else:
            bits.append("正在读取…")
        if p.text_length():
            bits.append(f'正文 {p.text_length()} 字')
        return "　".join([b for b in bits if b])

    n = len(nav.posts)
    bits = ["社区"]
    if source_label:
        bits.append(source_label)
    if nav.channel and nav.channel != "全部":
        bits.append(nav.channel)
    bits.append(f'第 {nav.index + 1}/{n} 条' if n else "没有内容")
    if nav.more:
        bits.append("还有更多")
    return "　".join(bits)


def post_needle(post):
    """列表里定位一条帖子用的文本：标题那一行，**和渲染一样先截断**。

    渲染会把长标题截到 `TITLE_CLIP`，定位这里要是拿完整标题去文档里找，长标题的帖子
    就永远高亮不上、鼠标点也不认（探针抓过这种"渲染和定位两套文本"的漂移）。"""
    return _clip(post.title, TITLE_CLIP) if post is not None else ""


def comment_needle(comment, nav=None):
    """详情里定位一条评论用的文本（表头那一行）。"""
    if comment is None:
        return ""
    if getattr(comment, "more_for", ""):
        return _comment_text(comment)          # "还有 N 条回复"那行：定位用它自己的字
    reply_to = nav.reply_to_of(comment) if nav is not None else ""
    return _comment_header(comment, reply_to)


def item_needles(nav):
    """当前视图里"每条一段定位文本"，按**文档顺序**排列：[(对象, 类型, 文本)]。

    界面层拿它做两件事：把 extra selection 落到当前这一条上；把鼠标点到的块号换算成
    "点的是哪一条"。共用同一个序列，就不会出现"渲染里找得到、定位里找不到"这种漂移。
    """
    if nav is None:
        return []
    if nav.view == "detail":
        return [(c, "comment", comment_needle(c, nav))
                for c, _d, _p in nav.visible_comments()]
    return [(p, "post", post_needle(p)) for p in nav.posts]


def selection_needle(nav):
    """当前选中项在文档里的定位文本——界面层靠它把 extra selection 落到那一条上。

    列表用标题（标题那一行就是标题本身）；评论用表头那一行（作者 时间 赞），
    折叠角的"展开/收起"后缀在它后面，所以前缀匹配照样命中。

    两个坑，都是探针抓出来的：
      - **不要**改成"渲染时插一个 ▸ 标记"：HTML 是上一次渲染的产物，光标一动标记就指向
        了错误的那条。选中状态只属于 extra selection，每动一次重算一次。
      - 表头文本必须和渲染共用一份拼装（`_comment_header`）：两边各拼一遍就会漂移；
        顺带记一笔，Qt 的 HTML 解析会把 `　`(U+3000) 折成半角空格，用全角空格当分隔符
        拼出来的定位文本在文档里根本找不到。
    """
    if nav.view == "detail":
        return comment_needle(nav.current_comment(), nav)
    return post_needle(nav.selected())
