"""小红书的 `x-s` 签名（纯 Python，只用标准库）。

**为什么需要这一层**：小红书的评论接口（`/api/sns/web/v2/comment/page`）不接受裸 Cookie——
每个请求都要带 `x-s` / `x-t` / `x-s-common` 三个头。`x-s` 是把"接口路径 + body + Cookie 里的
`a1` + 毫秒时间戳 + 自增序号"喂进它自己那套算法算出来的；`x-t` 就是那个时间戳；
`x-s-common` 是一段同样算法产出的指纹（base64 的 JSON）。

**这份实现是从哪儿来的**：照着 VS Code 摸鱼插件 TouchFish（`ylw.touchfish`，MIT）里那份
**纯 JS** 实现逐行搬过来的（`dist/extension.js` 里的 `XhsClient` / `signTier` / `xsCommon` /
`generateB1` / `DsFetcher`）。它没有 wasm、没有二次签名服务，全是 md5 + 定长异或 + 自定义
base64 表，所以搬得动。常量（字符表、密钥流、版本号）原样抄自它的 `CryptoConfig`。

**这份实现能干什么、不能干什么**：
- 能：算 `x-s` / `x-t` / `x-s-common` / `x-b3-traceid` / `x-xray-traceid`，让"读评论"这类
  **只读**请求通过；
- 不能：没有 Cookie 里的 `a1` 就签不出来（算法直接吃它），所以这个来源仍然是"要 Cookie"那一档；
- 不做：发帖 / 点赞 / 关注这些写操作（`x-rap-param` 那条路没搬，见文件末尾的说明）。
  微信读书正文那一层也不做，原因见 `weread.py`。

对外只有三个函数：`sign()`（一次签名的全部产物）、`headers_for()`（拼成请求头）、
`ds_pair()`（拿它那个 `ds` 参数，带 15 分钟缓存，拿不到就退回时间戳）。
"""

import base64
import hashlib
import json
import re
import time
import zlib

# --------------------------------------------------------------- 常量（抄自 TouchFish）
# 版本号、字符表、密钥流都原样来自 TouchFish 的 CryptoConfig；
# 它们跟着小红书前端换版本一起变，所以这里注明出处，方便以后对齐。
CONFIG = {
    'SIGN_VERSION': '4.4.3',
    'APP_ID': 'xhs-pc-web',
    'PLATFORM': 'Windows',
    'WEB_BUILD': '6.47.2',
    'B1_SDK_VERSION': '3.5.4',
    'PUBLIC_USERAGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
    'XS_ALPHABET': 'ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5',
    'MNS_ALPHABET': 'MfgqrsbcyzPQRStuvC7mn501HIJBo2DEFTKdeNOwxWXYZap89+/A4UVLhijkl63G',
    'B1_RC4_KEY': 'xhswebmplfbt',
    'ENV_CONST': 1352,
    'ENV_FP_TAIL': (249, 65, 103, 103, 201, 181, 129, 99, 94, 7, 68, 250, 132, 21),
}

# MNS_KEYSTREAM_B64：`x-s` 里那段定长异或用的密钥流（base64 → 原始字节）
KEYSTREAM = base64.b64decode('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')

# 进程启动时刻（毫秒）：明文里要带它，JS 版也是构造时取一次
LOADTS = int(time.time() * 1000)
DEVICE_TAG = "a3"


M32 = 0xFFFFFFFF


def _s32(v):
    """JS 的 `| 0`：把无符号 32 位当有符号看（`x-s-common` 里那个 CRC 可能是负数）。"""
    v &= M32
    return v - 0x100000000 if v & 0x80000000 else v


def _u32le(v):
    v &= M32
    return bytes((v & 255, (v >> 8) & 255, (v >> 16) & 255, (v >> 24) & 255))


def _u64le(v):
    return bytes((int(v) >> (8 * i)) & 255 for i in range(8))


def _rotl(x, n):
    x &= M32
    return ((x << n) | (x >> (32 - n))) & M32


def _md5(data):
    return hashlib.md5(data).digest()


def _utf8(text):
    return str(text).encode("utf-8")


def _b64_custom(data, alphabet):
    """标准 base64（含 `=` 补位）换一张字母表——和小红书 JS 版的 `b64Custom` 等价。"""
    std = base64.b64encode(bytes(data)).decode("ascii")
    src = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    return std.translate(str.maketrans(src, alphabet))


def _xor_key(data, key):
    """按位异或；密钥比数据短时，多出来的字节按"异或 0"处理（JS 里 `x ^ undefined` 就是它）。"""
    return bytes(b ^ (key[i] if i < len(key) else 0) for i, b in enumerate(data))


# ---------------------------------------------------------------- 指纹散列（它文档里叫 dsf）

def _dsf(data):
    """16 字节的定长散列：先用 8 字节一组混，再收尾混一轮。"""
    n = len(data)

    def rd(i):
        return int.from_bytes(data[i:i + 4], "little")

    r = (1831565813 ^ n) & M32
    s = (461845907 ^ ((n << 8) & M32)) & M32
    o = (2246822507 ^ ((n << 16) & M32)) & M32
    i4 = (3266489909 ^ ((n << 24) & M32)) & M32
    i = 0
    while i + 8 <= n:
        t1, t2 = rd(i), rd(i + 4)
        r = _rotl(((r + t1) & M32) ^ o, 7)
        s = _rotl((((s ^ t1) & M32) + i4) & M32, 11)
        o = _rotl(((o + t2) & M32) ^ r, 13)
        i4 = _rotl((((i4 ^ t2) & M32) + s) & M32, 17)
        i += 8
    r = (r ^ n) & M32
    s = (s ^ r) & M32
    o = (o + s) & M32
    i4 = (i4 ^ o) & M32
    r, s, o, i4 = _rotl(r, 9), _rotl(s, 13), _rotl(o, 17), _rotl(i4, 19)
    r = (r + o) & M32
    s = (s ^ i4) & M32
    o = (o + r) & M32
    i4 = (i4 ^ s) & M32
    return b"".join(_u32le(x) for x in (r, s, o, i4))


def _env_fp(n, tail):
    """`[1, 115^n] + tail` —— 环境指纹那一段。"""
    return bytes((1, 115 ^ (n & 255))) + bytes(tail)


# ---------------------------------------------------------------- 明文与 x-s

def _plaintext(api, data, a1, ts, loadts, seq, version, full_len=None):
    """把签名要吃的所有东西拼成一串字节（JS 里那个 `packPlaintext`）。"""
    n = version & 255
    data_s = "" if data is None else (data if isinstance(data, str) else json.dumps(
        data, separators=(",", ":"), ensure_ascii=False))
    full = _utf8(str(api) + data_s)
    md5_api = _md5(_utf8(api))
    e = _dsf(_u64le(ts) + md5_api)
    b = bytes(x ^ n for x in e)
    md5_full = _md5(full)
    out = bytearray()
    out += bytes((121, 104, 96, 41))
    out += _u32le(version)
    out += _u64le(ts)
    out += _u64le(loadts)
    out += _u32le(seq)
    out += _u32le(CONFIG["ENV_CONST"])
    out += _u32le(len(full) if full_len is None else full_len)
    out += bytes(x ^ n for x in md5_full[:8])
    out += bytes((len(_utf8(a1)) & 255,))
    out += _utf8(a1)
    app = _utf8(CONFIG["APP_ID"])
    out += bytes((len(app) & 255,))
    out += app
    out += _env_fp(n, CONFIG["ENV_FP_TAIL"])
    tag = _utf8(DEVICE_TAG)
    out += bytes((len(tag) & 255,))
    out += tag
    out += bytes((16,))
    out += b
    return bytes(out)


def sign_xs(api, data, a1, ts, seq, version, loadts=None):
    """→ `(xs, md5_full_hex)`。`xs` 就是 `x-s` 那个头的值（`mns0301_` 开头）。"""
    if loadts is None:
        loadts = LOADTS
    plain = _plaintext(api, data, a1, ts, loadts, seq, version)
    xs = "mns0301_" + _b64_custom(_xor_key(plain, KEYSTREAM), CONFIG["MNS_ALPHABET"])
    data_s = "" if data is None else (data if isinstance(data, str) else json.dumps(
        data, separators=(",", ":"), ensure_ascii=False))
    return xs, _md5(_utf8(str(api) + data_s)).hex()


# ---------------------------------------------------------------- 其他几个头

_CRC_TABLE = None


def _crc_table():
    global _CRC_TABLE
    if _CRC_TABLE is None:
        t = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = ((c >> 1) ^ 0xEDB88320) if (c & 1) else (c >> 1)
            t.append(c & M32)
        _CRC_TABLE = t
    return _CRC_TABLE


def gens9(text):
    """`x-s-common` 里的 `x9`：它自己那版 CRC（结果按有符号 32 位给，可能是负数）。"""
    table = _crc_table()
    t = M32
    for ch in _utf8(text):
        t = table[(t ^ ch) & 255] ^ (t >> 8)
    return _s32(0xEDB88320 ^ ((~t) & M32))


def _rc4(data, key):
    """`x-b3-traceid` 那一套里的 RC4（`b1` 用它把 JSON 搅一遍）。"""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 255
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) & 255])
    return bytes(out)


_B1_KEYS = ('x33', 'x34', 'x35', 'x36', 'x37', 'x38', 'x39', 'x42', 'x43', 'x44', 'x45', 'x46', 'x48', 'x49', 'x50', 'x51', 'x52', 'x82', 'x84')      # 字段顺序（b1 的 JSON 顺序会影响结果，所以从 bundle 里导出）
_B1_FIXED = {'x33': '0', 'x34': '0', 'x35': '0', 'x36': '2', 'x37': '0|0|0|0|0|0|0|0|0|1|0|0|1|0|0|0|0|1|1|0|0|0|0|0', 'x38': '0|0|1|0|1|0|0|0|0|0|1|0|1|0|1|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0', 'x39': '22', 'x42': '3.5.4', 'x43': 'Canvas not supported', 'x45': '__SEC_CAV__1-1-1-1-1|', 'x46': 'false', 'x48': '', 'x49': '{list:[],type:}', 'x50': '131,88,103', 'x51': '', 'x52': '', 'x82': '1|_BHjFmfUMEtxhI|_AUuXfEG27Xa3x|__xhsPendingNotePoint25300ReportMap|__xhsReportedNotePoint25300RecordMap|setImDebugMode|anti_hp_sign_config|__rap_app_id__|__rap_report__|__rap_last_sign_cost__|__rap_last_transform_cost__|__rap_hijack_installed__|__ed1a7dddf7c818e4bd|__hp_xhs_search_input_state__|__h'}    # 除 x44/x84 外的固定字段，也原样导出
_B1_ENV_HEAD = '{mt:{to:'
_B1_ENV_TAIL = '},m:{me:50,mm:50,md:1,mu:1,c:1},k:{},p:{ulr:1,ps:1,f:3,b:3,vc:0,rs:1,sc:0},st:{h:0,f:1,kr:0},ft:{ae:3.3656015629507223,ak:6.231183732330187,cdr:0.4096814442007536,bf:{ar:0.6210873146622735,fr:7.158084478545331},fi:2151.1}}'


def _js_num(v):
    """按 JS 的 `${Number}` 那样打数字：浮点用最短往返表示（和 Python 的 repr 一致）。"""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v) if isinstance(v, float) else str(v)


def _b1_env(now):
    """`x84`：一段环境快照，只有 `mt.to` 跟时间走（JS 里是 `now - 2151.1`）。"""
    return _B1_ENV_HEAD + _js_num(float(now) - 2151.1) + _B1_ENV_TAIL

def generate_b1(now=None):
    """`b1`：一份浏览器指纹，RC4 搅完再自定义 base64。"""
    now = int(time.time() * 1000) if now is None else int(now)
    fields = dict(_B1_FIXED)
    fields["x44"] = str(now)
    fields["x84"] = _b1_env(now)
    ordered = {k: fields[k] for k in _B1_KEYS if k in fields}
    raw = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
    blob = _rc4(_utf8(raw), _utf8(CONFIG["B1_RC4_KEY"]))
    # 它把 RC4 出来的**字节**当字符串又按 UTF-8 编了一遍：凡是 >=0x80 的字节都会胀成两个字节。
    # 少这一步 base64 会短一截（1248 对 1884），服务端直接不认——这个坑是拿 JS 版逐字节比出来的。
    return _b64_custom(blob.decode("latin-1").encode("utf-8"), CONFIG["XS_ALPHABET"])


def generate_xb3_traceid(n=16):
    import random
    return "".join(random.choice("abcdef0123456789") for _ in range(n))


def generate_xray_traceid():
    import random
    hi = ((int(time.time() * 1000) << 23) | random.randint(0, 8388607)) & 0xFFFFFFFFFFFFFFFF
    lo = random.getrandbits(64)
    return "%016x%016x" % (hi, lo)


def xs_common(a1, xs, b1, ds_pair_value, sign_count=0, web_build=None):
    """`x-s-common`：一段 base64 的 JSON 指纹。

    两个容易搞混的字段：`x8` 装 `b1`（那份浏览器指纹），`x12` 装 `ds` 对
    （`"<dsllt>;<ds>"`）。它们**不是同一个东西**——当初我把它们并成一个参数，
    算出来的串长了一倍，服务端直接不认。
    """
    u = {
        "s0": 5, "s1": "",
        "x0": "1",
        "x1": str(CONFIG["SIGN_VERSION"]),
        "x2": str(CONFIG["PLATFORM"]),
        "x3": str(CONFIG["APP_ID"]),
        "x4": str(web_build or CONFIG["WEB_BUILD"]),
        "x5": a1,
        "x6": "", "x7": "",
        "x8": None if not a1 else b1,
        "x9": gens9("null" if not a1 else b1),
        "x10": int(sign_count or 0),
        "x11": "normal",
        "x12": str(ds_pair_value or ""),
    }
    return _b64_custom(_utf8(json.dumps(u, separators=(",", ":"), ensure_ascii=False)),
                       CONFIG["XS_ALPHABET"])


# ---------------------------------------------------------------- ds 参数

DS_URL = "https://as.xiaohongshu.com/api/sec/v1/ds?appId=xhs-pc-web"
_DS_RE = re.compile(r"function\s+getdss\s*\(\s*\)\s*\{\s*return\s+'(\d+)'")
_ds_state = {"value": None, "fetched_at": 0, "llt": 0}


def ds_pair(ts=None, fetch=None):
    """`"<dsllt>;<ds>"` —— 签名里要用的一段"设备态"。拿不到就退回时间戳，不阻塞。

    `fetch` 是个 `fetch(url) -> text` 的可调用对象（探针里注入假的），不给就直接发 HTTP。
    """
    now = int(time.time() * 1000) if ts is None else int(ts)
    if not _ds_state["value"] or now - _ds_state["fetched_at"] >= 300000:
        text = ""
        try:
            if fetch is not None:
                text = fetch(DS_URL) or ""
            else:
                import urllib.request
                req = urllib.request.Request(DS_URL, headers={
                    "User-Agent": CONFIG["PUBLIC_USERAGENT"],
                    "Referer": "https://www.xiaohongshu.com/",
                    "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    text = r.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        m = _DS_RE.search(text or "")
        _ds_state["value"] = m.group(1) if m else str(now)
        _ds_state["fetched_at"] = now
    if not _ds_state["llt"] or now - _ds_state["llt"] >= 900000:
        _ds_state["llt"] = now
    return "%d;%s" % (_ds_state["llt"], _ds_state["value"])


# ---------------------------------------------------------------- 对外：一次签名

def a1_from_cookie(cookie):
    """从 `a1=...` 里取签名要用的那一段（没有就返回空串，签出来的东西服务端不认）。"""
    for part in str(cookie or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == "a1":
                return v.strip()
    return ""


_seq = {"n": 1, "count": 0}


def sign(path, data="", cookie="", method="GET", fetch=None, ts=None, web_build=None):
    """对一次请求签名 → 一串请求头（`x-s` / `x-t` / `x-s-common` / 两个 trace id）。

    `path` 要**带上查询串**（签名算的就是这一整串，顺序也不能变）。
    `web_build` 传 Cookie 里那个 `webBuild` 更贴近浏览器（它自己前端就是这么取）。
    """
    a1 = a1_from_cookie(cookie)
    now = int(time.time() * 1000) if ts is None else int(ts)
    seq = _seq["n"]
    _seq["n"] += 1
    count = _seq["count"]
    _seq["count"] += 1
    version = (hashlib.md5(_utf8(now)).digest()[0] << 24) ^ (seq * 2654435761 & M32)
    xs, _md5_full = sign_xs(path, data, a1, now, seq, version & M32)
    pair = ds_pair(now, fetch=fetch)
    b1 = generate_b1(now)
    return {
        "xs": xs,
        "xt": str(now),
        "xs_common": xs_common(a1, xs, b1, pair, count, web_build=web_build),
        "x_b3_traceid": generate_xb3_traceid(16),
        "x_xray_traceid": generate_xray_traceid(),
        "md5_full": _md5_full,
    }


def headers_for(sign_obj, cookie="", host="edith.xiaohongshu.com"):
    """把那串签名拼成真的请求头（顺序和字段都照 TouchFish 那份来）。"""
    h = {
        "Cookie": cookie,
        "User-Agent": CONFIG["PUBLIC_USERAGENT"],
        "Content-Type": "application/json;charset=UTF-8",
        "x-s": sign_obj["xs"],
        "x-t": sign_obj["xt"],
        "x-s-common": sign_obj["xs_common"],
        "xsecappid": CONFIG["APP_ID"],
        "authority": host,
        "referer": "https://www.xiaohongshu.com/",
        "accept": "application/json, text/plain, */*",
        "origin": "https://www.xiaohongshu.com",
    }
    if sign_obj.get("x_b3_traceid"):
        h["x-b3-traceid"] = sign_obj["x_b3_traceid"]
    if sign_obj.get("x_xray_traceid"):
        h["x-xray-traceid"] = sign_obj["x_xray_traceid"]
    return h


def path_with_query(path, params):
    """小红书的查询串是**不转义**直接拼的（签名算的就是这一串），所以这里也不转义。"""
    if not params:
        return path
    parts = []
    for k, v in params.items():
        parts.append("%s=%s" % (k, "" if v is None else str(v)))
    return path + "?" + "&".join(parts)


# 边缘说明：`x-rap-param`（写操作/部分搜索接口要）没搬——它依赖 `CryptoConfig.RAP_SBOX`
# 和另一套包装，而这几个来源我们只做"读"。真要做写操作，先在这里补 `generate_rap_param`。
