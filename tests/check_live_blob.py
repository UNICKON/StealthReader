# -*- coding: utf-8 -*-
"""线上只读核对：网页「上传书籍」那条接口在真 Worker 上通不通。

    python tests/check_live_blob.py

只做 `POST /v1/blob/check`：拿本机书架上的一本书算 sha256，问线上"这本有了吗"。
**不传任何字节、不写任何对象**，所以不会往云端塞测试书。
"""
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")   # 不带 UA 会被 CF 边缘按 1010 拒掉

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s  << %s" % (name, extra))


def pick_book():
    cfg = json.load(io.open(os.path.join(SR, "config.json"), encoding="utf-8"))
    best = None
    for base in [cfg.get("shelf_dir")] + list((cfg.get("local_book_positions") or {}).keys()):
        if not base:
            continue
        cands = [base] if os.path.isfile(base) else []
        if os.path.isdir(base):
            cands = [os.path.join(base, n) for n in os.listdir(base) if n.lower().endswith(".epub")]
        for p in cands:
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size > 300 * 1024 and (best is None or size < best[0]):
                best = (size, p)
    return best[1] if best else None


def main():
    cfg = json.load(io.open(os.path.join(SR, "config.json"), encoding="utf-8"))
    sync = cfg.get("sync") or {}
    tok = sync.get("token")
    base = str(sync.get("base_url") or "").rstrip("/")
    if not tok or not base:
        print("config.json 里没有 sync.token/base_url，跳过")
        return 0

    path = pick_book()
    if not path:
        print("找不到可用的 epub 素材，跳过")
        return 0
    data = io.open(path, "rb").read()
    sha = hashlib.sha256(data).hexdigest()
    print("素材：%s（%d 字节，sha=%s…）" % (os.path.basename(path), len(data), sha[:16]))

    body = json.dumps({"sha256": [sha]}).encode("utf-8")
    req = urllib.request.Request(base + "/v1/blob/check", data=body, method="POST",
                                 headers={"User-Agent": UA, "Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            got = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        check("POST /v1/blob/check 活着（HTTP %d）" % e.code, False, e.code)
        print("\n%d 通过 / %d 失败" % (PASS, FAIL))
        return 1

    check("POST /v1/blob/check 认 Bearer token（ok=%s）" % got.get("ok"), bool(got.get("ok")), got)
    check("本机这本书线上已经有了（missing 里没有它）", sha not in (got.get("missing") or []),
          got.get("missing"))
    check("线上的大小和本地一致（%s）" % got.get("sizes"), (got.get("sizes") or {}).get(sha) == len(data),
          "%s vs %d" % ((got.get("sizes") or {}).get(sha), len(data)))

    # 随手验一下"没登录不让传"：不带 token 应该是 401
    req2 = urllib.request.Request(base + "/v1/blob/check", data=body, method="POST",
                                  headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req2, timeout=20)
        check("不带 token 传书被拒（401）", False, "居然通了")
    except urllib.error.HTTPError as e:
        check("不带 token 传书被拒（HTTP %d）" % e.code, e.code == 401, e.code)

    print("\n%d 通过 / %d 失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
