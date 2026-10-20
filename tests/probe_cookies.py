# -*- coding: utf-8 -*-
"""feed_cookies 的探针：用一份**自己造的** Chromium profile 证明解密这条路是通的。

真机上 Edge 在跑的时候 Cookie 库是硬锁的（读不了，也不该偷偷去读），所以这里不碰真浏览器：
在临时目录里造 Local State + Cookies 库（v10 = AES-256-GCM，密钥由 DPAPI 保护），
再让 `chromium_roots()` 指向它。附带验域名过滤、v20 的"读不了"、cookies.txt 解析。
"""
import base64
import io
import os
import shutil
import sqlite3
import sys
import tempfile

SR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SR, "libs"))
sys.path.insert(0, SR)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import feed_cookies as fc                                     # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  <" + str(extra) + ">") if not cond else ""))
    if not cond:
        FAIL.append(name)


print("======== feed_cookies 探针 ========")

AES_KEY = os.urandom(32)
tmp = tempfile.mkdtemp(prefix="srprobe_cookie_")
user_data = os.path.join(tmp, "User Data")
profile = os.path.join(user_data, "Default", "Network")
os.makedirs(profile, exist_ok=True)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # noqa: E402


def make_v10(text):
    nonce = os.urandom(12)
    ct = AESGCM(AES_KEY).encrypt(nonce, text.encode("utf-8"), None)
    return b"v10" + nonce + ct


def make_row(host, name, text):
    return (host, name, "", make_v10(text), "/")


# ---- Local State（密钥用 DPAPI 包起来，和真 Chrome 一样）
key = fc.dpapi_protect(AES_KEY) if hasattr(fc, "dpapi_protect") else None
check("有 dpapi_protect（造键用）", key is not None)
with io.open(os.path.join(user_data, "Local State"), "w", encoding="utf-8") as f:
    import json
    json.dump({"os_crypt": {"encrypted_key":
                            base64.b64encode(b"DPAPI" + (key or b"")).decode()}}, f)

# ---- Cookies 库
db = os.path.join(profile, "Cookies")
con = sqlite3.connect(db)
con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
            "encrypted_value BLOB, path TEXT)")
con.execute("INSERT INTO cookies VALUES (?,?,?,?,?)", make_row(".zhihu.com", "z_c0", "zhihu-token"))
con.execute("INSERT INTO cookies VALUES (?,?,?,?,?)", make_row("www.zhihu.com", "d_c0", "dc0-value"))
con.execute("INSERT INTO cookies VALUES (?,?,?,?,?)", make_row(".example.com", "nope", "不该被读到"))
con.execute("INSERT INTO cookies VALUES (?,?,?,?,?)", make_row(".xiaohongshu.com", "a1", "xhs-a1"))
con.execute("INSERT INTO cookies VALUES (?,?,?,?,?)",
            (".weibo.com", "SUB", "", b"v20" + b"\x00" * 40, "/"))
con.commit()
con.close()

_roots = fc.chromium_roots
_firefox = fc.firefox_root
fc.chromium_roots = lambda: [("FakeChromium", user_data)]
fc.firefox_root = lambda: ""
try:
    found, notes = fc.scan()
finally:
    fc.chromium_roots = _roots
    fc.firefox_root = _firefox

zh = found.get("zhihu", {})
cookie = zh.get("cookie", "")
check("v10 的 Cookie 解得开（知乎）", "z_c0=zhihu-token" in cookie, cookie)
check("同一个来源的多个域名都收进来", "d_c0=dc0-value" in cookie, cookie)
check("不相关域名的 Cookie 一条都不碰", "nope" not in cookie, cookie)
check("小红书那条也在", "a1=xhs-a1" in found.get("xhs", {}).get("cookie", ""),
      found.get("xhs"))
check("记下了是哪家浏览器/profile", zh.get("browser") == "FakeChromium" and zh.get("profile"),
      zh)
check("v20 不假装成功（微博报读不了）", "weibo" not in found and any("v20" in n for n in notes),
      notes)

# ---- 域名匹配
check("域名匹配：子域算、别的域不算",
      fc._host_match("www.zhihu.com", ["zhihu.com"]) is True
      and fc._host_match("notzhihu.com", ["zhihu.com"]) is False)

# ---- cookies.txt（插件导出的 Netscape 格式）
netscape = ("# Netscape HTTP Cookie File\n"
            ".zhihu.com\tTRUE\t/\tTRUE\t0\tz_c0\tnetscape-token\n"
            ".example.com\tTRUE\t/\tFALSE\t0\tzzz\tbad\n")
txt = os.path.join(tmp, "cookies.txt")
with io.open(txt, "w", encoding="utf-8") as f:
    f.write(netscape)
got = fc.from_netscape_file(txt)
check("cookies.txt 能读进来（只要用得到的那几个域名）",
      "z_c0=netscape-token" in got.get("zhihu", "") and "zzz" not in str(got), got)

# ---- 占用检测（真的锁不住文件，就验一下函数对不存在的路径的态度）
check("读不了的库算作被占用", fc._path_locked(os.path.join(tmp, "没有这个文件")) is True)

# ---- 说明行
line = fc.describe({"zhihu": {"browser": "Edge"}}, ["Edge 正在运行"])
check("说明行是人话（来源 + 浏览器 + 原因）", "zhihu" in line and "Edge" in line, line)

fc.chromium_roots = lambda: []
fc.firefox_root = lambda: ""
try:
    found2, notes2 = fc.scan()
finally:
    fc.chromium_roots = _roots
    fc.firefox_root = _firefox
check("一台浏览器都没有时说得明白",
      found2 == {} and any("没找到" in n for n in notes2), notes2)

shutil.rmtree(tmp, ignore_errors=True)

print("\n================")
print("通过 %d / %d" % (0 if FAIL else 1, 1) if False else
      ("失败 %d 项：%s" % (len(FAIL), FAIL) if FAIL else "全部通过"))
sys.exit(1 if FAIL else 0)
