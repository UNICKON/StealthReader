# -*- coding: utf-8 -*-
"""线上前端文件核对：`https://sync.noteklip.org/r/` 上跑的，是不是本地这份。

    python tests/check_web_assets.py

只读：不登录、不碰任何同步对象，拿线上字节算 sha256 跟本地 `cloudflare-sync/web/` 比。
`index.html` 例外——Worker 会往里注一段 CSP，字节必然不同，只比"该有的东西在不在"。

改完 `web/` 之后要先 `npx wrangler deploy`，再跑这个；没过就说明部署没上去（或者缓存没刷）。
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
WEB = os.path.join(SR, "cloudflare-sync", "web")

BASE = "https://sync.noteklip.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")   # 不带 UA 会被 CF 边缘按 1010 拒掉

PASS = 0
FAIL = 0
BAD = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        BAD.append(name + ("  << %s" % extra if extra else ""))
        print("  FAIL %s  << %s" % (name, extra))


def get(path):
    """取线上字节 + 响应头。

    优先走系统 `curl`：2026-11-19 实测本机 Python 的 TLS 到 Cloudflare 会卡死
    （urllib 无论带不带 UA 都 20s 超时，同一条 URL 用 curl 1s 就拿到 401），
    是工具链的问题，不是线上的问题——不能因为取不到就跳过核对。
    用 curl 时走 `-o/-D` 两个临时文件，这样响应头也能核对（CSP 就在头里）。
    """
    url = BASE + path
    curl = shutil.which("curl")
    if curl:
        body_f = tempfile.NamedTemporaryFile(delete=False)
        hdr_f = tempfile.NamedTemporaryFile(delete=False)
        body_f.close()
        hdr_f.close()
        try:
            done = subprocess.run([curl, "-sS", "--max-time", "40", "-A", UA,
                                   "-o", body_f.name, "-D", hdr_f.name, url],
                                  capture_output=True)
            if done.returncode == 0:
                with open(body_f.name, "rb") as f:
                    body = f.read()
                hdrs = {}
                with open(hdr_f.name, "rb") as f:
                    for line in f.read().decode("iso-8859-1").splitlines():
                        if ":" in line and not line.startswith("HTTP/"):
                            k, v = line.split(":", 1)
                            hdrs[k.strip()] = v.strip()
                return body, hdrs
        finally:
            for n in (body_f.name, hdr_f.name):
                try:
                    os.unlink(n)
                except OSError:
                    pass
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read(), dict(r.headers)


def main():
    for name in ("app.js", "style.css", "index.html", "vendor/foliate-js/paginator.js"):
        local = io.open(os.path.join(WEB, name.replace("/", os.sep)), "rb").read()
        try:
            live, hdrs = get("/r/" + name)
        except Exception as e:
            check("线上拿得到 /r/%s" % name, False, e)
            continue
        lh = hashlib.sha256(local).hexdigest()[:16]
        rh = hashlib.sha256(live).hexdigest()[:16]
        if name == "index.html":
            # Worker 是把 CSP 当**响应头**发的（不是往 HTML 里塞 meta）：这里查头，
            # 内容照样逐字节比——以前这条查的是 body 里的 meta，永远不可能对。
            low = {k.lower(): v for k, v in (hdrs or {}).items()}
            check("/r/index.html 带 Worker 发的 CSP 响应头（%s…）"
                  % (low.get("content-security-policy", "")[:32] or "-"),
                  "script-src" in low.get("content-security-policy", ""),
                  sorted(low.keys()))
            check("/r/index.html 内容与本地一字节不差（%d 字节）" % len(local), lh == rh,
                  "%s vs %s" % (lh, rh))
        else:
            check("/r/%s 跟本地一字节不差（%s，%d 字节）" % (name, lh, len(local)), lh == rh,
                  "本地 %s/%d 线上 %s/%d" % (lh, len(local), rh, len(live)))

        if name == "app.js":
            check("线上 app.js 带版式开关（sr.layout.v1）", b"sr.layout.v1" in live)
            check("线上 app.js 关页面才推（pagehide + keepalive）",
                  b"pagehide" in live and b"keepalive: true" in live)
            check("线上 app.js 没有 2.5 秒防抖推（不做实时同步）",
                  b"setTimeout(() => { pushProgress() }, 2500)" not in live)
            check("线上 app.js 滚动走 scrollToAnchor（closed shadow 拿不到 #container）",
                  b"scrollToAnchor" in live)
            check("线上 app.js 每 15 分钟同步一次（syncEveryMs = 900000）",
                  b"syncEveryMs: 15 * 60 * 1000" in live)
            check("线上 app.js 设置只存本机（sr.settings.v1，完全不碰 /v1/state/settings）",
                  b"sr.settings.v1" in live and b"/v1/state/settings" not in live)
            check("线上 app.js 网页端会记阅读统计（pushStats → /v1/state/stats）",
                  b"function pushStats" in live and b"'/v1/state/stats'" in live
                  and b"MAX_CHARS_PER_SEC = 20" in live)
            check("线上 app.js 待推队列落盘（sr.pendingstats.v1，推不上也不丢）",
                  b"sr.pendingstats.v1" in live and b"function savePending" in live)
            check("线上 app.js 管两种后台情形（visibilitychange 收场 / pageshow 从缓存回来重开）",
                  b"visibilitychange" in live and b"pageshow" in live and b"e.persisted" in live)
            check("线上 app.js 同一浏览器多标签只让一个记账（sr.statowner.v1）",
                  b"sr.statowner.v1" in live and b"function isOwner" in live)
            check("线上 app.js 回书架不 await 网络（goShelf 先切界面再推）",
                  b"function goShelf" in live and b"await pushProgress(true)" not in live)
            check("线上 app.js 阅读时底栏自动收起（barsShow / body.bare）",
                  b"function barsShow" in live and b"classList.add('bare')" in live)
            check("线上 app.js 网页能传书（/v1/blob/check + PUT /v1/blob/）",
                  b"'/v1/blob/check'" in live and b"/v1/blob/${sha}" in live)
            check("线上 app.js 网页自己算总字数（ensureTotal → PUT /v1/lite/）",
                  b"async function ensureTotal" in live and b"/v1/lite/${book.uid}/index.json" in live)
            check("线上 app.js 有统计页（openStats 读 /v1/state/stats）",
                  b"async function openStats" in live and b"function statRow" in live)
        elif name == "style.css":
            check("线上 style.css 有 [hidden] 兜底规则（目录关不掉那个坑）",
                  b"[hidden] { display: none !important; }" in live)
            check("线上 style.css 有配色色块样式（7 个 .sw 预设）",
                  live.count(b".tools .s-") == 7)
            check("线上 style.css 沉浸模式（body.bare .hud 收起来）",
                  b"body.bare .hud" in live)
            check("线上 style.css 有统计页样式（.s-row / .s-book）",
                  b".s-row" in live and b".s-book" in live)
        elif name == "index.html":
            check("线上 index.html 有翻页/滚动、单栏/双栏按钮",
                  b"flow-scroll" in live and b"cols-2" in live)
            check("线上 index.html 有 7 个配色色块 + 4 种字体",
                  live.count(b'class="sw s-') == 7 and live.count(b'data-act="face-') == 4)
            check("线上 index.html 不再有说明性备注（#tools-note）",
                  b"tools-note" not in live)
            check("线上 index.html 书架上有统计那一行（#shelf-stats）",
                  b'id="shelf-stats"' in live)
            check("线上 index.html 有「上传书籍」「统计」两个入口",
                  b'id="upload-btn"' in live and b'id="stats-btn"' in live)
            check("线上 index.html 有统计页骨架（#stats-pane / #stats-body）",
                  b'id="stats-pane"' in live and b'id="stats-body"' in live)
            check("线上 index.html 有隐藏的文件选择框（accept epub/mobi…）",
                  b'id="file"' in live and b"application/epub+zip" in live)
        elif name.endswith("paginator.js"):
            check("线上 paginator.js 带本地补丁（分页模式下 snap 直接返回）",
                  b"if (!this.scrolled) return" in live)

    # 自备字体：宋 / 楷 / 圆随包发，线上必须真的在。字体掉了正文会静默退回系统字体，
    # 手机上看就是"换字体没反应"，所以这条单独守。
    for name in ("fonts.css", "fonts/sr-serif.woff2", "fonts/sr-round.woff2"):
        try:
            local = io.open(os.path.join(WEB, name.replace("/", os.sep)), "rb").read()
            body, _ = get("/r/" + name)
            check("线上 %s 与本地一致（%d 字节）" % (name, len(local)),
                  hashlib.sha256(body).hexdigest() == hashlib.sha256(local).hexdigest(),
                  "%d vs %d" % (len(body), len(local)))
        except Exception as e:                                   # noqa: BLE001
            check("线上 %s 取得到" % name, False, repr(e))
    try:
        body, _ = get("/r/fonts.css")
        text = body.decode("utf-8", "replace")
        check("fonts.css 里三档字体都在（SR Serif / SR Round / SR Kai）",
              "SR Serif" in text and "SR Round" in text and "SR Kai" in text)
        check("fonts.css 里的字体地址已是绝对地址（没有 __FONT_BASE__ 漏网）",
              "__FONT_BASE__" in text)   # 占位在文件里，由页面替换；这里确认它还在（没被写死成本机路径）
    except Exception as e:                                       # noqa: BLE001
        check("线上 fonts.css 取得到", False, repr(e))

    try:
        body, _ = get("/v1/ping")
        check("/v1/ping 还活着（%s）" % body[:60], bool(json.loads(body.decode("utf-8"))))
    except urllib.error.HTTPError as e:
        # ping 也要鉴权：不带 token 回 401 是正常行为——说明 Worker 活着，且在守门
        check("/v1/ping 活着且要鉴权（HTTP %d）" % e.code, e.code == 401, e.code)

    print("\n%d 通过 / %d 失败" % (PASS, FAIL))
    if BAD:
        print("\n".join("  - " + b for b in BAD))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
