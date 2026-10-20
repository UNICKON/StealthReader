"""从本机浏览器里找 Cookie —— 只读、只写 `feed_auth.json`。

为什么要有这一层：整套东西里唯一必须"离开程序"的一步就是手贴 Cookie。人要么去开发者工具
里翻，要么干脆放弃那几个来源。这个模块做的事很窄：**把本机浏览器已经有的那几条 Cookie
读出来，拼成 `feed_auth.json` 里那个格式**，读不出来就照实说读不出来（不猜、不编）。

三条边界，写在这里也写在界面上：

1. **只读**：打开浏览器的 Cookie 库是 `mode=ro&immutable=1`，不写、不加锁、不改配置。
2. **只挑用得上**：只取下面 `SOURCE_DOMAINS` 里那几个域名；其余站点的 Cookie 一条都不碰。
3. **只写一个文件**：结果交给调用方去合并进 `feed_auth.json`（已经 gitignore），
   不往任何地方发。

浏览器覆盖面（Windows）：

- **Firefox**：`cookies.sqlite` 里是明文，直接查。
- **Chrome / Edge / Brave / Vivaldi**：`Network/Cookies`（老版本在 profile 根下），
  值是 `v10`/`v11` 前缀的 AES-256-GCM，密钥在 `Local State` 的 `os_crypt.encrypted_key`
  里、由 Windows DPAPI 保护 → 用 `CryptUnprotectData` 解出密钥再解值。
- **v20（Chrome 127+ 的 App-Bound 加密）**：密钥在 Chrome 自己的进程里，拿不到。
  这种情况**不假装成功**：报"这家浏览器读不了，请手动贴一次"。

所以真机上三种结果都可能出现，界面要照实区分：找到并生效 / 找到但没用（域名对了、接口
还是说没登录，多半过期）/ 读不出来（v20 或没装）。
"""

import base64
import ctypes
import glob
import json
import os
import shutil
import sqlite3
import tempfile
from ctypes import wintypes

# 每个来源要哪几个域名（只取这些，其余一条都不碰）。
SOURCE_DOMAINS = {
    "zhihu": ["zhihu.com"],
    "weibo": ["weibo.com", "weibo.cn"],
    "nga": ["nga.cn", "ngabbs.com", "nga.178.com"],
    "xhs": ["xiaohongshu.com"],
    "linuxdo": ["linux.do"],
    "v2ex": ["v2ex.com"],
    "hupu": ["hupu.com"],
    "weread": ["weread.qq.com", "qq.com"],
    "ithome": ["ithome.com"],
}

# 界面上"能不能自动找"的名单（`SOURCE_DOMAINS` 的键），顺序就是设置里那一列的顺序
AUTO_KEYS = ("zhihu", "weibo", "nga", "xhs", "linuxdo", "v2ex", "hupu", "weread")


class CookieReadError(Exception):
    """读不出来（加密读不了、没有这个 profile…），消息直接给人看。"""


# --------------------------------------------------------------------------- DPAPI
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(blob):
    """Windows DPAPI 解密（当前用户）。纯 ctypes，不依赖 pywin32。"""
    if not blob:
        raise CookieReadError("空的加密串")
    buf = ctypes.create_string_buffer(blob, len(blob))
    in_blob = _BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise CookieReadError("DPAPI 解不开（换机器/换用户了？）")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _aes_gcm(key, nonce, payload, tag):
    """AES-256-GCM 解密。优先 cryptography，其次 pycryptodome；都没有就报清楚。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        AESGCM = None
    if AESGCM is not None:
        try:
            return AESGCM(key).decrypt(nonce, payload + tag, None)
        except Exception as exc:                   # 认证失败 = 密钥不对
            raise CookieReadError("解不开（%s）" % type(exc).__name__)
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise CookieReadError("这台机器上没有 AES 库（cryptography / pycryptodome）")
    try:
        return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(payload, tag)
    except Exception as exc:
        raise CookieReadError("解不开（%s）" % type(exc).__name__)


def dpapi_protect(data):
    """DPAPI 加密（当前用户）。**只给探针造数据用**，程序本身不加密任何东西。"""
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    in_blob = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise CookieReadError("DPAPI 加密失败")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


# --------------------------------------------------------------------------- 公共
def _host_match(host, domains):
    host = (host or "").lstrip(".").lower()
    for dom in domains:
        dom = dom.lstrip(".").lower()
        if host == dom or host.endswith("." + dom):
            return True
    return False


def _open_sqlite(path):
    """只读打开（immutable 不碰 WAL，也不给正在用的浏览器加锁）；不行就复制一份再读。"""
    uri = "file:%s?mode=ro&immutable=1" % str(path).replace("\\", "/")
    try:
        return sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        tmp_dir = tempfile.mkdtemp(prefix="srck")
        tmp = os.path.join(tmp_dir, "Cookies")
        shutil.copy2(path, tmp)
        for suffix in ("-wal", "-shm"):
            src = str(path) + suffix
            if os.path.isfile(src):
                try:
                    shutil.copy2(src, tmp + suffix)
                except OSError:
                    pass
        return sqlite3.connect(tmp, timeout=2)


def _cookie_header(rows, domains, decrypt):
    """rows: [(host, name, value, encrypted, path)] → "a=1; b=2"（只要这几个域名）"""
    pairs = []
    for host, name, value, enc, path in rows:
        if not _host_match(host, domains):
            continue
        text = value or ""
        if not text and enc:
            try:
                text = decrypt(enc)
            except CookieReadError:
                raise
            except Exception:
                continue
        if text and name:
            pairs.append((host.lstrip("."), path or "/", name, text))
    if not pairs:
        return ""
    # 长的 path 先写（和浏览器发 Cookie 的顺序一致）；同名只留第一条
    pairs.sort(key=lambda item: (-len(item[1]), item[0], item[2]))
    seen, bits = set(), []
    for _host, _path, name, value in pairs:
        if name in seen:
            continue
        seen.add(name)
        bits.append("%s=%s" % (name, value))
    return "; ".join(bits)


# --------------------------------------------------------------------------- Chromium
def chromium_roots():
    """[(浏览器名, User Data 目录)]，只留真实存在的。"""
    local = os.environ.get("LOCALAPPDATA") or ""
    cands = [
        ("Edge", os.path.join(local, "Microsoft", "Edge", "User Data")),
        ("Chrome", os.path.join(local, "Google", "Chrome", "User Data")),
        ("Brave", os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data")),
        ("Vivaldi", os.path.join(local, "Vivaldi", "User Data")),
    ]
    return [(name, path) for name, path in cands if path and os.path.isdir(path)]


def chromium_master_key(user_data):
    """从 Local State 里取 DPAPI 保护的 AES 密钥（拿不到返回 None + 原因）。"""
    state = os.path.join(user_data, "Local State")
    try:
        with open(state, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        raise CookieReadError("读不到 Local State")
    enc = ((data.get("os_crypt") or {}).get("encrypted_key") or "")
    if not enc:
        raise CookieReadError("Local State 里没有 os_crypt.encrypted_key")
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        raise CookieReadError("密钥格式不是 DPAPI")
    return dpapi_unprotect(raw[5:])


def chromium_profiles(user_data):
    """真正用过的 profile（有 Cookies 库就算），默认 profile 排前面。"""
    out = []
    try:
        entries = sorted(os.listdir(user_data))
    except OSError:
        return out
    for entry in entries:
        base = os.path.join(user_data, entry)
        if not os.path.isdir(base):
            continue
        for sub in (os.path.join(base, "Network"), base):
            db = os.path.join(sub, "Cookies")
            if os.path.isfile(db):
                out.append((entry, db))
                break
    out.sort(key=lambda item: 0 if item[0] in ("Default", "Profile 1") else 1)
    return out


def chromium_rows(user_data):
    """[(profile, rows)]：库里所有 Cookie 的原始行（一次读完，多个来源共用）。"""
    out = []
    for profile, db in chromium_profiles(user_data):
        con = None
        try:
            con = _open_sqlite(db)
            rows = con.execute(
                "SELECT host_key, name, value, encrypted_value, path FROM cookies").fetchall()
            out.append((profile, rows))
        except Exception:
            continue
        finally:
            if con is not None:
                con.close()
    return out


def _chromium_decoder(key):
    def decrypt(blob):
        prefix = bytes(blob[:3])
        if prefix in (b"v10", b"v11"):
            if key is None:
                raise CookieReadError("没有可用的密钥")
            return _aes_gcm(key, blob[3:15], blob[15:-16], blob[-16:]).decode("utf-8", "replace")
        if prefix == b"v20":
            raise CookieReadError("Chrome 127+ 的 App-Bound 加密（v20）读不了，请手动贴一次")
        return dpapi_unprotect(blob).decode("utf-8", "replace")
    return decrypt


# --------------------------------------------------------------------------- Firefox
def firefox_root():
    app = os.environ.get("APPDATA") or ""
    root = os.path.join(app, "Mozilla", "Firefox", "Profiles")
    return root if os.path.isdir(root) else ""


def firefox_rows():
    """[(profile, rows)]，Firefox 的 Cookie 是明文。"""
    root = firefox_root()
    out = []
    if not root:
        return out
    for db in glob.glob(os.path.join(root, "*", "cookies.sqlite")):
        con = None
        try:
            con = _open_sqlite(db)
            rows = con.execute("SELECT host, name, value, '', path FROM moz_cookies").fetchall()
            out.append((os.path.basename(os.path.dirname(db)), rows))
        except Exception:
            continue
        finally:
            if con is not None:
                con.close()
    return out


# --------------------------------------------------------------------------- 对外
def _path_locked(path):
    """这个库是不是被正在跑的浏览器独占着（Windows 上是硬锁，读都读不了）。"""
    try:
        with open(path, "rb") as f:
            f.read(16)
        return False
    except OSError:
        return True


def locked_browsers():
    """[(浏览器名, 是不是在跑)]——在跑的那个库打不开，得先关浏览器。"""
    out = []
    for name, user_data in chromium_roots():
        for _profile, db in chromium_profiles(user_data):
            if _path_locked(db):
                out.append(name)
                break
    if firefox_root():
        for db in glob.glob(os.path.join(firefox_root(), "*", "cookies.sqlite")):
            if _path_locked(db):
                out.append("Firefox")
                break
    return out


def close_browser(name):
    """优雅关掉这个浏览器（`taskkill` 不带 /F = 发关闭消息，会话仍然能恢复）。

    只在人**明确同意**之后调用（见设置页那句"关掉 X 再读一次？"）。返回 (ok, 说明)。
    """
    exe = {"Edge": "msedge.exe", "Chrome": "chrome.exe", "Brave": "brave.exe",
           "Vivaldi": "vivaldi.exe", "Firefox": "firefox.exe"}.get(name)
    if not exe:
        return False, "不认识的浏览器：%s" % name
    try:
        import subprocess
        r = subprocess.run(["taskkill", "/IM", exe], capture_output=True, timeout=15)
    except Exception as exc:
        return False, "关不掉（%s）" % type(exc).__name__
    text = (r.stdout or b"").decode("gbk", "replace") + (r.stderr or b"").decode("gbk", "replace")
    if r.returncode != 0 and "not found" not in text.lower() and "没有" not in text:
        # 没在跑也算成功（库这时自然是开的）
        return "not running" in text.lower(), text.strip()[:120] or "关不掉"
    return True, "已让 %s 关闭（标签页下次打开会恢复）" % name


def parse_netscape(text):
    """解析浏览器插件导出的 cookies.txt（Netscape 格式）→ {域名: [(name, value), ...]}"""
    out = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, _secure, _expiry, name, value = parts[:7]
        out.setdefault(domain.lstrip(".").lower(), []).append((name, value, path or "/"))
    return out


def from_netscape_file(path):
    """从 cookies.txt 里挑出用得上的来源：{来源 key: cookie 串}。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        table = parse_netscape(f.read())
    found = {}
    for src, domains in SOURCE_DOMAINS.items():
        rows = []
        for host, entries in table.items():
            if _host_match(host, domains):
                for name, value, path in entries:
                    rows.append((host, name, value, "", path))
        if rows:
            header = _cookie_header(rows, domains, lambda _b: "")
            if header:
                found[src] = header
    return found


def scan(keys=None):
    """找一遍。返回 (结果, 说明)。

    结果 = {来源 key: {"cookie": "...", "browser": "Edge", "profile": "Default"}}
    说明 = [给人看的一句话, ...]（读不出来的原因都在这儿，界面照实显示）

    同一个来源在多家浏览器里都有时，**取前面那家**（Edge → Chrome → Brave → Vivaldi → Firefox）；
    已经找到过的来源不再换人。
    """
    keys = [k for k in (keys or AUTO_KEYS) if k in SOURCE_DOMAINS]
    found, notes = {}, []
    tried_any = False

    for name, user_data in chromium_roots():
        tried_any = True
        locked = any(_path_locked(db) for _p, db in chromium_profiles(user_data))
        if locked:
            notes.append("%s 正在运行：Cookie 库被它独占，关掉 %s 再试一次" % (name, name))
            continue
        try:
            key = chromium_master_key(user_data)
        except CookieReadError as exc:
            notes.append("%s：%s" % (name, exc))
            key = None
        profiles = chromium_rows(user_data)
        if not profiles:
            notes.append("%s：没找到 Cookie 库" % name)
            continue
        decrypt = _chromium_decoder(key)
        for profile, rows in profiles:
            for src in keys:
                if src in found:
                    continue
                try:
                    header = _cookie_header(rows, SOURCE_DOMAINS[src], decrypt)
                except CookieReadError as exc:
                    if not any(name in n for n in notes):
                        notes.append("%s：%s" % (name, exc))
                    continue
                if header:
                    found[src] = {"cookie": header, "browser": name, "profile": profile}

    missing = [k for k in keys if k not in found]
    if missing and firefox_root():
        tried_any = True
        for profile, rows in firefox_rows():
            for src in missing:
                if src in found:
                    continue
                header = _cookie_header(rows, SOURCE_DOMAINS[src], lambda _b: "")
                if header:
                    found[src] = {"cookie": header, "browser": "Firefox", "profile": profile}

    if not tried_any:
        notes.append("这台机器上没找到 Chrome / Edge / Firefox / Brave 的 profile")
    return found, notes


def describe(found, notes, enabled=None):
    """把 scan() 的结果说成一句人话（设置窗口的提示行用它）。"""
    bits = []
    for src in sorted(found):
        if enabled is not None and src not in enabled:
            continue
        bits.append("%s（%s）" % (src, found[src]["browser"]))
    head = ("找到 " + "、".join(bits)) if bits else "没找到能用的 Cookie"
    if notes:
        head += "｜" + "；".join(notes[:2])
    return head


if __name__ == "__main__":                      # 手动试一次用（不进程序）
    got, why = scan()
    for k, v in got.items():
        print(k, "→", v["browser"], v.get("profile"), "长度", len(v["cookie"]))
    for line in why:
        print("说明：", line)
    print(describe(got, why))
