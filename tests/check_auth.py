# -*- coding: utf-8 -*-
"""凭据自检：**故意联网**，回答一个具体问题——"我贴的这个 Cookie 到底管不管用"。

    python tests/check_auth.py                 # 只查 feed_auth.json 里配了的来源
    python tests/check_auth.py --all           # 连没配的也查一遍（看"不贴 Cookie 是什么样"）
    python tests/check_auth.py --only nga      # 只查一个来源

用法（贴 Cookie 的流程）：
  1. 浏览器里登录那个站 → F12 → Network → 随便点一个请求 → 复制请求头里的 `Cookie` 整行；
  2. 粘到应用目录的 `feed_auth.json`（这个文件在 .gitignore 里，**不会进版本库**）：
         {"nga": {"cookie": "贴在这里", "user_agent": "顺手把浏览器的 User-Agent 也贴过来"}}
     linux.do 尤其要贴 user_agent：它拿 Cookie 和 UA 一起校验；
  3. 跑这个脚本，看每个站到底是 200 还是 401/403/432。

**响应只写到 %TEMP%\\sr_auth_probe\\，绝不写进仓库**：带 Cookie 拿到的响应里有你的用户名、
头像地址这些东西，不该进版本库。要拿它当夹具，先自己看一遍再手动搬。

这个脚本不改任何配置、不发帖、不点赞，只读。
"""
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
AUTH_PATH = os.path.join(SR, "feed_auth.json")
OUT = os.path.join(os.environ["TEMP"], "sr_auth_probe")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

SAVED = []


def load_auth():
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("读 %s 失败：%s" % (AUTH_PATH, e))
        return {}


class Session(object):
    def __init__(self, key, cred, save):
        self.key = key
        self.cred = cred or {}
        self.cookie = str(self.cred.get("cookie") or "").strip()
        self.ua = str(self.cred.get("user_agent") or UA)
        self.save = save
        self.s = requests.Session()

    def req(self, label, url, headers=None, expect_json=True, save=None):
        h = {"User-Agent": self.ua, "Accept": "*/*, */*;q=0.8",
             "Accept-Language": "zh-CN,zh;q=0.9"}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        t0 = time.time()
        try:
            r = self.s.get(url, headers=h, timeout=20, allow_redirects=True)
        except Exception as e:
            print("    %-22s 连不上：%s: %s" % (label, type(e).__name__, e))
            return None
        dt = time.time() - t0
        body = r.content or b""
        kind = "JSON" if expect_json else "HTML"
        top = ""
        if expect_json:
            try:
                obj = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                obj = None
            if isinstance(obj, dict):
                top = "顶层键 " + ",".join(list(obj.keys())[:8])
            elif isinstance(obj, list):
                top = "顶层是数组，%d 项" % len(obj)
            else:
                head = body.decode("utf-8", "replace")[:60].replace("\n", " ")
                top = "不是 JSON（%s…）" % head
        print("    %-22s HTTP %-3d %7d 字节 %5.1fs  %s  %s"
              % (label, r.status_code, len(body), dt, kind, top))
        print("      %s" % verdict(r, body))
        if self.save and save:
            name = "%s_%s.%s" % (self.key, save, "json" if expect_json else "html")
            path = os.path.join(OUT, name)
            with open(path, "wb") as f:
                f.write(body)
            SAVED.append(path)
            print("      存到 %s" % path)
        return r


def verdict(r, body):
    """一句人话：这个返回对"我要不要登录"意味着什么。

    要点全在细节里：NGA 的 403 是 **GBK** 的"未登录"（只按 utf-8 解就是乱码）、
    微信读书用 `errCode:-2010` 表示没登录、微博的移动端接口直接回一个去登录页的 url。"""
    code = r.status_code
    raw = body or b""
    decoded = []
    for enc in ("utf-8", "gbk"):
        try:
            decoded.append(raw[:4000].decode(enc, "ignore"))
        except Exception:
            pass
    joined = " ".join(decoded)
    low = joined.lower()
    if code == 200:
        try:
            obj = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            obj = None
        if isinstance(obj, dict) and obj.get("errCode") not in (None, 0):
            return "要登录 / 没权限（errCode %s：%s）" % (obj.get("errCode"), obj.get("errMsg") or "")
        if isinstance(obj, dict) and obj.get("ok") not in (None, 1):
            return "接口回了 ok=%s（多半是在要登录：%s）" % (obj.get("ok"), obj.get("url") or "")
        if "passport." in joined or "/sso/signin" in joined:
            return "要登录（接口把人引向登录页）"
        return "通了" if len(raw) > 500 else "200，但返回体很小——看一眼内容"
    if code in (401, 432):
        return "要登录（Cookie 没配 / 过期了）"
    if code == 403:
        if "cf-mitigated" in str(r.headers).lower() or "cloudflare" in low or "just a moment" in low:
            return "被 Cloudflare 盾拦住（要带 cf_clearance，且 UA 必须和浏览器一致）"
        if "未登录" in joined or "请登录" in joined or "not logged" in low:
            return "要登录（403 未登录）：贴浏览器里的 Cookie 再试"
        return "被拒（403）：Cookie 不对、或者这个接口还要前端签名/额外参数"
    if code == 567:
        return "非标准 567：这个站的防爬占位页，脚本基本过不去"
    if code == 404:
        return "404：接口地址变了"
    if code >= 500:
        return "服务端错误（%d）" % code
    return "看返回体"


def check_zhihu(S, save):
    print("  知乎（热榜免登录；正文/评论要登录）")
    r = S.req("热榜", "https://api.zhihu.com/topstory/hot-list?limit=50&reverse_order=0",
              save="hotlist" if save else None)
    qid = ""
    if r is not None and r.status_code == 200:
        try:
            items = json.loads(r.content.decode("utf-8", "replace")).get("data") or []
            qid = str(((items[0] or {}).get("target") or {}).get("id") or "")
        except Exception:
            qid = ""
    if qid:
        S.req("问题 %s 的回答" % qid,
              "https://www.zhihu.com/api/v4/questions/%s/answers"
              "?include=data[*].content,excerpt,voteup_count,comment_count&limit=1&offset=0"
              % qid,
              headers={"Referer": "https://www.zhihu.com/question/%s" % qid,
                       "x-requested-with": "fetch"},
              save="answers" if save else None)


def check_weibo(S, save):
    print("  微博（热搜免登录；正文/评论要登录）")
    r = S.req("热搜", "https://weibo.com/ajax/side/hotSearch",
              headers={"Referer": "https://weibo.com/"}, save="hotsearch" if save else None)
    word = ""
    if r is not None and r.status_code == 200:
        try:
            items = (json.loads(r.content.decode("utf-8", "replace")).get("data") or {}).get("realtime") or []
            word = str((items[0] or {}).get("word") or "")
        except Exception:
            word = ""
    if word:
        from urllib.parse import quote
        S.req("搜索 %s" % word[:8],
              "https://m.weibo.cn/api/container/getIndex?containerid=" + quote("100103type=1&q=" + word, safe=""),
              headers={"Referer": "https://m.weibo.cn/", "X-Requested-With": "XMLHttpRequest"},
              save="msearch" if save else None)


def check_nga(S, save):
    print("  NGA（免登录完全进不去）")
    r = S.req("网事杂谈列表", "https://bbs.nga.cn/thread.php?fid=-7&__output=8",
              headers={"Referer": "https://bbs.nga.cn/"}, save="list" if save else None)
    tid = ""
    if r is not None and r.status_code == 200:
        try:
            node = (json.loads(r.content.decode("utf-8", "replace")).get("data") or {}).get("__T") or {}
            keys = sorted(node.keys(), key=lambda x: str(x))
            tid = str((node.get(keys[0]) or {}).get("tid") or "") if keys else ""
        except Exception:
            tid = ""
    if tid:
        S.req("帖子 %s" % tid, "https://bbs.nga.cn/read.php?tid=%s&__output=8" % tid,
              headers={"Referer": "https://bbs.nga.cn/"}, save="read" if save else None)


def check_linuxdo(S, save):
    print("  linux.do（Cloudflare 盾；Cookie 里要有 cf_clearance，UA 必须和浏览器一致）")
    S.req("最新", "https://linux.do/latest.json", save="latest" if save else None)


def check_v2ex(S, save):
    print("  V2EX（免登录公开 API；本机网络大概率连不上）")
    S.req("最热", "https://www.v2ex.com/api/topics/hot.json", save="hot" if save else None)


def check_weread(S, save):
    print("  微信读书（批次 F：书架 → 章节 → 喂进本地的书管线）")
    S.req("书架", "https://weread.qq.com/web/shelf/sync?synckey=0",
          headers={"Referer": "https://weread.qq.com/"},
          save="shelf" if save else None)


def check_xhs(S, save):
    print("  小红书（免登录只有空壳；带上 Cookie 看服务端渲不渲染）")
    for label, url in (("探索页", "https://www.xiaohongshu.com/explore"),
                       ("搜索页", "https://www.xiaohongshu.com/search_result?keyword=%E5%A5%BD%E7%89%A9")):
        r = S.req(label, url, expect_json=False, save=label if save else None)
        if r is not None and r.status_code == 200:
            body = r.content.decode("utf-8", "replace")
            has = [m for m in ("noteId", "note_id", "xsec_token") if m in body]
            print("      页面里带真实笔记数据的痕迹: %s"
                  % (", ".join(has) if has else "没有（还是空壳）"))


def check_chiphell(S, save):
    print("  Chiphell（下面每条路径都见过非标准 567 的防爬页）")
    for label, url in (("首页", "https://www.chiphell.com/"),
                       ("热门", "https://www.chiphell.com/forum.php?mod=guide&view=hot")):
        S.req(label, url, expect_json=False, save=label if save else None)


CHECKS = (("zhihu", check_zhihu), ("weibo", check_weibo), ("nga", check_nga),
          ("linuxdo", check_linuxdo), ("v2ex", check_v2ex),
          ("weread", check_weread), ("xhs", check_xhs), ("chiphell", check_chiphell))


def main():
    argv = sys.argv[1:]
    only = ""
    if "--only" in argv:
        i = argv.index("--only")
        only = argv[i + 1] if i + 1 < len(argv) else ""
    every = "--all" in argv
    save = "--no-save" not in argv
    auth = load_auth()
    if save:
        os.makedirs(OUT, exist_ok=True)
    print("凭据文件 %s：%s" % (AUTH_PATH, "存在" if os.path.exists(AUTH_PATH) else "不存在"))
    print("里面配了：%s\n" % (", ".join(sorted(auth.keys())) or "（一个都没有）"))
    todo = [c for c in CHECKS if (not only or c[0] == only)
            and (every or only or auth.get(c[0]))]
    if not todo:
        print("没有要查的来源。先在 feed_auth.json 里贴一个 Cookie，或者加 --all 看「不贴是什么样」。")
        return 0
    for key, fn in todo:
        cred = auth.get(key) if isinstance(auth.get(key), dict) else {}
        if not cred.get("cookie"):
            print("  %s：没配 Cookie（下面这些请求是不带的）" % key)
        print("  === %s ===" % key)
        fn(Session(key, cred, save), save)
        print("")
    print("提示：401/403/432 是「没登录或 Cookie 过期」，不是脚本坏了；"
          "200 但内容看不懂，把 %s 里对应的文件发我，我按真响应收解析。" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
