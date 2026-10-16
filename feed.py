# -*- coding: utf-8 -*-
"""社区统一流：数据模型 + 来源适配器 + HTML 渲染 + 导航逻辑。

设计见 docs/community-feed-design.md：**所有来源只写一个适配器**（列表 / 正文 / 评论三件事），
界面层只认 Post / Comment / Block 这三个结构，所以加一个来源 = 加一个适配器，界面一行不改。

这一层刻意**不 import Qt、不发任何网络请求**：
- 探针可以在没有界面、没有网络的情况下反复跑（批次 A 就是这么验收的）；
- main.py 只做两件事：把这里渲染出的 HTML 塞进 text_edit，把 baseUrl 钉到图片目录。

批次 A 只有 DemoSource（读 feed_demo/ 里的 JSON）：零配置、离线、给真人看效果用。
"""

import html
import json
import os

# 正文块的种类（Block.kind）
BLOCK_P = "p"
BLOCK_H = "h"
BLOCK_QUOTE = "quote"
BLOCK_CODE = "code"
BLOCK_IMG = "img"
BLOCK_LIST = "list"
KINDS = (BLOCK_P, BLOCK_H, BLOCK_QUOTE, BLOCK_CODE, BLOCK_IMG, BLOCK_LIST)

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

    __slots__ = ("id", "author", "created", "score", "body", "replies")

    def __init__(self, cid, author="", created="", score=0, body=None, replies=None):
        self.id = str(cid)
        self.author = author or ""
        self.created = created or ""
        self.score = int(score or 0)
        self.body = [Block.from_dict(b) for b in (body or [])]
        self.replies = list(replies or [])

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            d.get("id", ""), d.get("author", ""), d.get("created", ""), d.get("score", 0),
            d.get("body", []),
            [cls.from_dict(r) for r in (d.get("replies") or [])],
        )

    def to_dict(self):
        return {
            "id": self.id, "author": self.author, "created": self.created, "score": self.score,
            "body": [b.to_dict() for b in self.body],
            "replies": [r.to_dict() for r in self.replies],
        }

    def total(self):
        """自己 + 所有后代。折叠时页脚要显示"这里还藏着几条"。"""
        return 1 + sum(r.total() for r in self.replies)


class Post(object):
    """一条帖子。列表页只用前半截字段，详情页才有 body；图片一律走本地缓存目录。"""

    __slots__ = ("source", "source_label", "channel", "id", "title", "author", "created",
                 "score", "n_comments", "cover", "excerpt", "url", "body", "tags", "comments")

    def __init__(self, source="", channel="", pid="", title="", author="", created="", score=0,
                 n_comments=0, cover="", excerpt="", url="", body=None, tags=None, comments=None,
                 source_label=""):
        self.source = source
        self.source_label = source_label or source
        self.channel = channel or ""
        self.id = str(pid)
        self.title = title or ""
        self.author = author or ""
        self.created = created or ""
        self.score = int(score or 0)
        self.n_comments = int(n_comments or 0)
        self.cover = cover or ""          # 相对图片来源目录的文件名
        self.excerpt = excerpt or ""
        self.url = url or ""
        self.body = [Block.from_dict(b) for b in (body or [])]
        self.tags = list(tags or [])
        self.comments = [Comment.from_dict(c) for c in (comments or [])]

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            source=d.get("source", ""), source_label=d.get("source_label", ""),
            channel=d.get("channel", ""), pid=d.get("id", ""), title=d.get("title", ""),
            author=d.get("author", ""), created=d.get("created", ""), score=d.get("score", 0),
            n_comments=d.get("n_comments", 0), cover=d.get("cover", ""),
            excerpt=d.get("excerpt", ""), url=d.get("url", ""), body=d.get("body", []),
            tags=d.get("tags", []), comments=d.get("comments", []),
        )

    def to_dict(self):
        return {
            "source": self.source, "source_label": self.source_label, "channel": self.channel,
            "id": self.id, "title": self.title, "author": self.author, "created": self.created,
            "score": self.score, "n_comments": self.n_comments, "cover": self.cover,
            "excerpt": self.excerpt, "url": self.url,
            "body": [b.to_dict() for b in self.body], "tags": self.tags,
            "comments": [c.to_dict() for c in self.comments],
        }

    def text_length(self):
        return sum(len(b.text) for b in self.body)


def comment_forest(tree):
    """把评论树拍平成 [(comment, depth, parent)]，顺序就是显示顺序。

    上下移动（J/K）、折叠、页脚统计都在这条序列上做，所以它必须是**唯一的顺序来源**。"""
    out = []

    def walk(nodes, depth):
        for c in nodes:
            out.append((c, depth, None))
            walk(c.replies, depth + 1)

    walk(tree or [], 0)
    return out


def descendant_ids(node):
    out = []
    for r in node.replies:
        out.append(r.id)
        out.extend(descendant_ids(r))
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
        seen = []
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


def build_sources(demo_root):
    """以后每加一个来源就在这里登记一个适配器；界面的来源筛选直接由它生成。"""
    return [DemoSource(demo_root)]


# --------------------------------------------------------------------------- 导航（纯逻辑）

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
        return True

    def set_detail(self, post, comments):
        self.post = post
        self.roots = list(comments or [])
        self.flat = comment_forest(self.roots)
        self.detail_loaded = True
        if self.c_index >= len(self.flat):
            self.c_index = 0

    def back(self):
        if self.view == "list":
            return False
        self.view = "list"
        self.post = None
        self.roots = []
        self.flat = []
        self.c_index = 0
        self.collapsed = set()
        self.detail_loaded = False
        return True

    def move_comment(self, step):
        if not self.flat:
            return False
        new = max(0, min(len(self.flat) - 1, self.c_index + step))
        if new == self.c_index:
            return False
        self.c_index = new
        return True

    def current_comment(self):
        if 0 <= self.c_index < len(self.flat):
            return self.flat[self.c_index][0]
        return None

    def toggle_collapse(self):
        """折叠/展开当前选中那条（只有带回复的才折叠得动）。"""
        cur = self.current_comment()
        if cur is None or not cur.replies:
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
        """按折叠状态过滤后的显示序列：**被折叠节点的整个子树都不显示**。

        折叠过滤只有一个实现（这里）。之前想在拍平序列上按"祖先是否被折叠"筛，
        结果是折叠了父节点、子节点照旧露出来——这种错在界面上很难看出来，
        所以直接递归：遇到折叠节点就不再往里走。"""
        out = []

        def walk(nodes, depth):
            for c in nodes:
                out.append((c, depth, None))
                if c.id not in self.collapsed:
                    walk(c.replies, depth + 1)

        walk(self.roots, 0)
        return out

    def comment_roots(self):
        return self.roots

    def comment_stats(self):
        total = sum(c.total() for c in self.roots)
        return total, len(self.visible_comments()), len(self.collapsed)


# --------------------------------------------------------------------------- 渲染

def esc(text):
    return html.escape(str(text or ""), quote=False)


def _nl(text):
    return esc(text).replace("\n", "<br>")


def theme_from_config(cfg, width_px=420):
    """把阅读器的排版设置折成渲染主题：社区页和书页**同一套字号/配色**，
    换字号、改透明度时社区页跟着一起变——"接进现有排版"就是这个意思。"""
    base = cfg.get("text_color", "#d8d8d8")
    return {
        "text": base,
        "mark": _mix(base, 1.0, boost=38),
        "dim": _mix(base, 0.62),
        "faint": _mix(base, 0.45),
        "accent": _mix(base, 1.0, accent=True),
        "size": int(cfg.get("font_size", 14)),
        "family": cfg.get("font_family", "Microsoft YaHei"),
        "lowkey": bool(cfg.get("feed_lowkey", False)),
        "width": max(200, int(width_px)),
    }


def _mix(color, factor, boost=0, accent=False):
    """把正文色调成亮/暗档，省得社区页自己硬编码一套配色（换主题时会和正文脱节）。"""
    try:
        c = (color or "#d8d8d8").strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except Exception:
        r, g, b = 216, 216, 216
    if accent:
        r, g, b = r + 8, g + 34, b + 62
    elif boost:
        r, g, b = r + boost, g + boost, b + boost
    else:
        r, g, b = int(r * factor), int(g * factor), int(b * factor)
    return "#%02x%02x%02x" % (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def _meta_line(post, theme):
    """列表/详情的元信息行：来源（带色）+ 栏目 · 作者 · 时间。

    分隔符用中点、不用全角空格：Qt 的 HTML 解析会把 `　` 折成半角空格，最后挤成一团；
    中点既不会被折，也比空格更接近"reddit 那种一行信息"的读感。"""
    src = post.source_label or post.source
    bits = [b for b in [post.channel, post.author, post.created] if b]
    head = f'<span style="color:{theme["accent"]};">{esc(src)}</span>' if src else ""
    tail = " · ".join(esc(b) for b in bits)
    return (head + (" · " if head and tail else "") + tail)


def _img_tag(src, theme, max_w):
    w = max(80, min(theme["width"] - 40, max_w))
    return f'<img src="{esc(src)}" width="{w}">'


def render_list(posts, nav, theme):
    """混流卡片列表。

    **一律不用底色块**：靠缩进和字色区分。真人前面反馈过"这个区块不要用背景色，
    太显眼"——那条同样适用于这里，而且在工位上更像一份文档。

    这里也**不画"当前选中"的标记**：HTML 是上一次渲染的产物，光标一动标记就成了过期
    信息（"高亮跟着光标走"这件事只有 extra selection 做得到）。文档本身与选中无关，
    于是上下移动不必重排整篇 HTML。"""
    if not posts:
        return _wrap(theme, f'<p style="color:{theme["dim"]};">这个来源还没有内容。</p>')
    parts = []
    for p in posts:
        parts.append(f'<a name="post-{esc(p.id)}"></a>')
        parts.append(
            f'<p style="margin:13px 0 1px 0; color:{theme["text"]};'
            f' font-size:{theme["size"] + 1}px; font-weight:bold;">{esc(p.title)}</p>'
        )
        meta = _meta_line(p, theme)
        extra = []
        if p.score:
            extra.append(f'{p.score} 赞')
        if p.n_comments:
            extra.append(f'{p.n_comments} 评论')
        if extra:
            meta += (" · " if meta else "") + " · ".join(esc(e) for e in extra)
        if meta:
            # 缩进用 margin-left，不用前导空格：全角空格会被 Qt 折成半角，缩进等于没有
            parts.append(
                f'<p style="margin:0 0 1px 0; margin-left:14px; color:{theme["faint"]};'
                f' font-size:{max(11, theme["size"] - 2)}px;">{meta}</p>'
            )
        if p.excerpt:
            parts.append(
                f'<p style="margin:0 0 2px 0; margin-left:14px; color:{theme["dim"]};'
                f' font-size:{max(11, theme["size"] - 1)}px;">{_nl(p.excerpt)}</p>'
            )
        if p.cover:
            if theme["lowkey"]:
                parts.append(f'<p style="margin:0 0 0 14px; color:{theme["faint"]};'
                             f' font-size:11px;">[图]</p>')
            else:
                parts.append(f'<p style="margin:5px 0 2px 14px;">{_img_tag(p.cover, theme, 320)}</p>')
    return _wrap(theme, "".join(parts))


def render_detail(post, nav, theme):
    """帖子详情：标题 + 元信息 + 正文（我们自己的排版）+ 树状评论。"""
    if post is None:
        return _wrap(theme, f'<p style="color:{theme["dim"]};">没有选中帖子。</p>')
    parts = [
        f'<p style="margin:0 0 3px 0; color:{theme["mark"]}; font-size:{theme["size"] + 3}px;'
        f' font-weight:bold;">{esc(post.title)}</p>',
        f'<p style="margin:0 0 10px 0; color:{theme["faint"]};'
        f' font-size:{max(11, theme["size"] - 2)}px;">{_meta_line(post, theme)}</p>',
    ]
    parts.extend(_block_paragraphs(post.body, theme))
    total, _shown, nfold = nav.comment_stats() if nav.detail_loaded else (0, 0, 0)
    if total:
        head = f'评论 {total} 条' + (f'　已折叠 {nfold} 处' if nfold else "")
        parts.append(
            f'<p style="margin:18px 0 2px 0; color:{theme["dim"]};'
            f' font-size:{max(11, theme["size"] - 1)}px;">—— {esc(head)} ——</p>'
        )
        parts.append(render_comment_tree(nav, theme))
    elif nav.detail_loaded:
        parts.append(
            f'<p style="margin:18px 0 2px 0; color:{theme["faint"]};'
            f' font-size:{max(11, theme["size"] - 1)}px;">—— 没有评论 ——</p>'
        )
    return _wrap(theme, "".join(parts))


def _comment_header(c):
    """评论表头那一行的正文：作者 · 时间 · 赞。

    渲染和"定位选中项"用的是同一份拼装——两处各拼一遍必然漂移（探针就抓到过一次：
    渲染用全角空格、定位用半角，结果高亮永远落不上）。折叠角是状态，由调用方追加。"""
    bits = [c.author or "匿名"]
    if c.created:
        bits.append(c.created)
    if c.score:
        bits.append(f"{c.score} 赞")
    return " · ".join(bits)


def render_comment_tree(nav, theme):
    """树状评论：层级用缩进表示，折叠的节点显示"还藏着几条"。

    和列表一样，**"哪一条是当前的"不画在 HTML 里**（那是 extra selection 的事）；
    折叠角是数据本身的状态，所以照画。"""
    vis = nav.visible_comments()
    if not vis:
        return f'<p style="color:{theme["faint"]};">（评论都折叠了）</p>'
    parts = []
    for c, depth, _p in vis:
        indent = 14 * depth
        head = _comment_header(c)
        if c.replies:
            head += " · " + ("▸ 展开（藏着 %d 条）" % len(descendant_ids(c))
                             if c.id in nav.collapsed else "▾")
        parts.append(
            f'<p style="margin:9px 0 0 {indent}px; color:{theme["dim"]};'
            f' font-size:{max(11, theme["size"] - 2)}px;">{esc(head)}</p>'
        )
        inner = "".join(
            f'<p style="margin:1px 0 0 {indent + 12}px; color:{theme["text"]};'
            f' font-size:{max(11, theme["size"] - 1)}px;">{_block_inline(b, theme)}</p>'
            for b in (c.body or [])
        )
        parts.append(inner)
    return "".join(parts)


def _block_inline(b, theme):
    if b.kind == BLOCK_IMG:
        return (f'<span style="color:{theme["faint"]};">[图]</span>' if theme["lowkey"]
                else _img_tag(b.url or b.text, theme, 360))
    if b.kind == BLOCK_H:
        return f'<span style="font-weight:bold; color:{theme["mark"]};">{_nl(b.text)}</span>'
    if b.kind == BLOCK_QUOTE:
        return f'<span style="color:{theme["dim"]};">｜{_nl(b.text)}</span>'
    if b.kind == BLOCK_CODE:
        return f'<span style="font-family:Consolas,monospace; color:{theme["dim"]};">{_nl(b.text)}</span>'
    if b.kind == BLOCK_LIST:
        return f'<span>· {_nl(b.text)}</span>'
    return _nl(b.text)


def _block_paragraphs(blocks, theme):
    """正文块渲染成独立段落（每块一个 <p>，有自己的间距）。

    不用 <br> 把整篇串成一整段：那样段间距全丢，一屏密密麻麻，和书页排版完全两回事。"""
    out = []
    for b in blocks:
        if b.kind == BLOCK_IMG:
            if theme["lowkey"]:
                out.append(f'<p style="margin:8px 0; color:{theme["faint"]};">[图]</p>')
            else:
                out.append(f'<p style="margin:8px 0;">{_img_tag(b.url or b.text, theme, 420)}</p>')
            continue
        style = f'margin:0 0 9px 0; padding:0; font-size:{theme["size"]}px;'
        if b.kind == BLOCK_H:
            style += f' color:{theme["mark"]}; font-weight:bold;'
        elif b.kind == BLOCK_QUOTE:
            style += f' color:{theme["dim"]};'
        elif b.kind == BLOCK_CODE:
            style += f' color:{theme["dim"]}; font-family:Consolas,monospace;'
        elif b.kind == BLOCK_LIST:
            style += f' color:{theme["text"]};'
        else:
            style += f' color:{theme["text"]};'
        text = ("· " + b.text) if b.kind == BLOCK_LIST else b.text
        out.append(f'<p style="{style}">{_nl(text)}</p>')
    return out


def _wrap(theme, body):
    return f'<div style="font-family:{esc(theme["family"])}; color:{theme["text"]};">{body}</div>'


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
        c = nav.current_comment()
        return _comment_header(c) if c is not None else ""
    p = nav.selected()
    return p.title if p is not None else ""
