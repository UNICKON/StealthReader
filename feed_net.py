# -*- coding: utf-8 -*-
"""社区统一流的取数与缓存层：一个地方管网络、超时、限速、磁盘缓存、图片落地。

为什么单独一个文件：
- feed.py 是"数据模型 + 排版 + 导航"，刻意不碰网络；适配器（feed_sources.py）只管
  "把某个站的 JSON 变成 Post/Comment"。网络这一层单独放，谁都不用重复写超时和缓存。
- **缓存不是可选项**：工位上的网络时好时坏，来源挂了要能拿上一次的内容顶上，
  而且列表页反复进出不该每次都打人家的服务器（真人要求过低调，也包含"别太吵"）。

约定：
- 所有失败都抛 FeedNetError，**message 是给人看的中文短句**（页脚直接显示它）；
- 缓存命中就不发请求；`ttl=0` 表示永不过期（图片用这个）；
- 图片只落地不解析：Qt 的 QTextDocument 读不了 http，必须先存成本地文件。
"""

import hashlib
import json
import os
import re
import time

try:
    import requests
except Exception:                       # pragma: no cover - 环境缺 requests 时也不至于导入就炸
    requests = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_TIMEOUT = 12
HOST_MIN_INTERVAL = 0.25        # 同一台主机两次请求之间至少隔这么久
MAX_IMAGE_BYTES = 6 * 1024 * 1024
IMAGE_EXT_OK = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# 列表短缓存、正文长缓存：列表要新鲜，正文基本不变
TTL_LIST = 5 * 60
TTL_DETAIL = 24 * 60 * 60
TTL_FOREVER = 0


class FeedNetError(Exception):
    """取数失败。message 直接给真人看，所以一律是中文短句。"""


def _short_err(e):
    """把 requests 那一长串异常压成一句人话。"""
    name = type(e).__name__
    text = str(e)
    if "Name or service not known" in text or "getaddrinfo" in text or "NameResolution" in name:
        return "域名解析不了（可能没网）"
    if "timed out" in text.lower() or "Timeout" in name:
        return "连接超时"
    if "Connection refused" in text:
        return "连接被拒绝"
    if "SSL" in name or "SSLError" in text:
        return "证书/连接出错"
    if "ProxyError" in name:
        return "代理出错"
    return name


class Cache(object):
    """磁盘缓存：sha1(url) 当文件名，JSON 原样存，图片存二进制。

    用 URL 当键（而不是"来源+id"）：同一条内容换个接口拿也是同一份缓存，实现也简单。
    写的时候先写 .tmp 再替换——中途断电不会留下半个文件把缓存毒死。
    """

    def __init__(self, root):
        self.root = root
        self.json_dir = os.path.join(root, "json")
        self.img_dir = os.path.join(root, "img")
        self.bin_dir = os.path.join(root, "bin")
        for d in (self.json_dir, self.img_dir, self.bin_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass

    @staticmethod
    def key(url):
        return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()

    def _path(self, sub, url, ext=""):
        return os.path.join(sub, self.key(url) + ext)

    def read_json(self, url, ttl):
        path = self._path(self.json_dir, url, ".json")
        try:
            age = time.time() - os.path.getmtime(path)
            if ttl and age > ttl:
                return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def write_json(self, url, obj):
        path = self._path(self.json_dir, url, ".json")
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass

    def read_bytes(self, url, ttl):
        path = self._path(self.bin_dir, url, ".bin")
        try:
            age = time.time() - os.path.getmtime(path)
            if ttl and age > ttl:
                return None
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            return None

    def write_bytes(self, url, data):
        path = self._path(self.bin_dir, url, ".bin")
        try:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception:
            pass

    def image_name(self, url):
        """图片已经落地就返回文件名（相对 cache/img 的），否则空串。"""
        for ext in IMAGE_EXT_OK:
            name = self.key(url) + ext
            if os.path.exists(os.path.join(self.img_dir, name)):
                return name
        return ""

    def image_path(self, name):
        return os.path.join(self.img_dir, name)

    def clear(self):
        n = 0
        for d in (self.json_dir, self.bin_dir, self.img_dir):
            try:
                for f in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, f))
                        n += 1
                    except Exception:
                        pass
            except Exception:
                pass
        return n


def _says_login(r):
    """返回体里是不是在说"要登录"。utf-8 / gbk / \\uXXXX 转义三种写法都认。

    NGA 是 GBK（`{"error":{"0":"1:未登录"}}`），知乎是 utf-8，还有一些站把中文转义成 \\uXXXX。"""
    raw = (r.content or b"")[:4000]
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc, "ignore")
        except Exception:
            continue
        low = text.lower()
        if "未登录" in text or "请登录" in text or "not logged" in low or "login required" in low:
            return True
    escaped = raw.decode("ascii", "ignore").lower()
    return ("\\u672a\\u767b\\u5f55" in escaped          # 未登录
            or "\\u8bf7\\u767b\\u5f55" in escaped)      # 请登录


class Http(object):
    """带缓存和限速的取数器。每个来源一个实例（各自可以带自己的 Cookie/UA）。"""

    def __init__(self, cache=None, timeout=DEFAULT_TIMEOUT, headers=None, cookies=None,
                 user_agent=UA, min_interval=HOST_MIN_INTERVAL):
        self.cache = cache
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent,
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        self.headers.update(headers or {})
        self.cookies = dict(cookies or {})
        self.min_interval = min_interval
        self._last = {}                 # host -> 上次请求时间
        self._sess = None
        self.last_error = ""
        self.served_stale = False       # 这一次返回的是不是"断网时顶上的旧缓存"

    # -- 内部 -------------------------------------------------------------
    def _session(self):
        if requests is None:
            raise FeedNetError("缺少 requests 库")
        if self._sess is None:
            self._sess = requests.Session()
        return self._sess

    def _throttle(self, url):
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        now = time.time()
        wait = self.min_interval - (now - self._last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.time()

    def _get(self, url, headers=None, allow_redirects=True, timeout=None):
        self._throttle(url)
        h = dict(self.headers)
        h.update(headers or {})
        try:
            r = self._session().get(url, headers=h, cookies=self.cookies or None,
                                    timeout=timeout or self.timeout, allow_redirects=allow_redirects)
        except Exception as e:
            raise FeedNetError(_short_err(e))
        if r.status_code == 403 and "just a moment" in r.text[:2000].lower():
            raise FeedNetError("被 Cloudflare 盾拦住（要贴浏览器 Cookie）")
        if r.status_code == 401:
            raise FeedNetError("要登录（401）")
        if r.status_code == 432:
            # 微博的"要登录"用的是这个非标准码（实测 m.weibo.cn 返回 432 + 空体）
            raise FeedNetError("要登录（432）")
        if r.status_code == 403:
            # 403 有好几种：Cloudflare 盾（上面那条）、"没登录"、以及"没权限"。
            # 看一眼返回体再下结论——NGA 免登录时返回的就是 GBK 的 {"error":{"0":"1:未登录"}}，
            # 所以这里 utf-8 和 gbk 都试一遍（只按 utf-8 解会解成乱码，关键词就命不中了）。
            if _says_login(r):
                raise FeedNetError("要登录（403）")
            raise FeedNetError("被拒绝（403）")
        if r.status_code == 404:
            raise FeedNetError("接口不存在（404）")
        if r.status_code >= 400:
            raise FeedNetError("服务端返回 %d" % r.status_code)
        return r

    # -- 对外 -------------------------------------------------------------
    def get_json(self, url, ttl=TTL_LIST, headers=None, what="", timeout=None):
        """先看新鲜缓存，没有再请求。

        请求失败时**退回过期缓存**：工位上网络时好时坏，上一次拿到的列表比一片空白有用得多
        （这正是"缓存不是可选项"那条）。真的一点都没有，才把错误抛上去。"""
        if self.cache is not None:
            hit = self.cache.read_json(url, ttl)
            if hit is not None:
                self.served_stale = False
                return hit
        try:
            r = self._get(url, headers=headers, timeout=timeout)
            obj = r.json()
        except FeedNetError:
            stale = self.cache.read_json(url, TTL_FOREVER) if self.cache is not None else None
            if stale is not None:
                self.served_stale = True
                return stale
            raise
        except Exception:
            stale = self.cache.read_json(url, TTL_FOREVER) if self.cache is not None else None
            if stale is not None:
                self.served_stale = True
                return stale
            raise FeedNetError("%s返回的不是 JSON" % (what or ""))
        self.served_stale = False
        if self.cache is not None:
            self.cache.write_json(url, obj)
        return obj

    def get_bytes(self, url, ttl=TTL_FOREVER, headers=None, max_bytes=MAX_IMAGE_BYTES):
        if self.cache is not None:
            hit = self.cache.read_bytes(url, ttl)
            if hit is not None:
                return hit
        r = self._get(url, headers=headers)
        data = r.content
        if len(data) > max_bytes:
            raise FeedNetError("文件太大")
        if self.cache is not None:
            self.cache.write_bytes(url, data)
        return data

    def fetch_image(self, url):
        """把图片落到 cache/img，返回文件名（失败返回空串，绝不抛）。

        图片永远只是"锦上添花"：网络烂的时候主流程该照常出文字，不能因为一张图挂掉。
        """
        if not url or self.cache is None:
            return ""
        done = self.cache.image_name(url)
        if done:
            return done
        if url.startswith("//"):
            url = "https:" + url
        if not url.lower().startswith("http"):
            return ""
        try:
            data = self.get_bytes(url)
        except FeedNetError:
            return ""
        except Exception:
            return ""
        if not data or len(data) < 64:
            return ""
        ext = _sniff_ext(data, url)
        if not ext:
            return ""
        name = self.cache.key(url) + ext
        try:
            tmp = self.cache.image_path(name) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.cache.image_path(name))
        except Exception:
            return ""
        return name


def _sniff_ext(data, url=""):
    """按**内容**判图片类型（按扩展名判会被 CDN 的假后缀骗）。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ""
