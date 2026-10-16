# -*- coding: utf-8 -*-
"""把 VS Code 摸鱼插件（ylw.touchfish）里已配好的 Cookie 搬进 StealthReader 的 feed_auth.json。

只读插件的 settings.json，只写 feed_auth.json（那个文件在 .gitignore 里）。
**不打印任何 Cookie 内容**，只打印"有没有、多长、像不像那么回事"。
"""
import json
import os
import sys

VSCODE = os.path.join(os.environ["APPDATA"], "Code", "User", "settings.json")
HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
AUTH = os.path.join(SR, "feed_auth.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

# 插件设置项 -> 我们这边的来源 key
MAP = {
    "touchfish.zhihuCookie": "zhihu",
    "touchfish.ngaCookie": "nga",
    "touchfish.linuxDoCookie": "linuxdo",
    "touchfish.weiboCookie": "weibo",
    "touchfish.xhsCookie": "xhs",
    "touchfish.wereadCookie": "weread",
    "touchfish.v2exCookie": "v2ex",
    "touchfish.douyinCookie": "douyin",       # 我们不做抖音，留着只为"看得见"
}
# 几个"这儿应该有"的标记，用来判断这串 Cookie 是不是那个站的（只看布尔，不打印值）
MARK = {
    "zhihu": ["z_c0", "_zap", "d_c0"],
    "nga": ["ngaPassportUid", "ngaPassportCid", "guestJs"],
    "linuxdo": ["_t", "cf_clearance", "_cfuvid"],
    "weibo": ["SUB", "SUBP", "SSOLoginState"],
    "xhs": ["web_session", "a1", "webId"],
    "weread": ["wr_skey", "wr_vid"],
    "v2ex": ["A2", "V2EX_LANG"],
}


def main():
    apply_ = "--apply" in sys.argv
    if not os.path.exists(VSCODE):
        print("找不到 %s" % VSCODE)
        return 1
    with open(VSCODE, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    # VS Code 的 settings.json 允许注释和尾逗号，这里只做"能读就够"的宽松处理
    try:
        settings = json.loads(raw)
    except Exception:
        import re
        stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        settings = json.loads(stripped)

    auth = {}
    if os.path.exists(AUTH):
        try:
            with open(AUTH, "r", encoding="utf-8") as f:
                auth = json.load(f) or {}
        except Exception:
            auth = {}

    found, missing = [], []
    for key, src in sorted(MAP.items()):
        val = settings.get(key)
        if not isinstance(val, str) or not val.strip():
            missing.append(src)
            continue
        val = val.strip()
        marks = {m: (m in val) for m in MARK.get(src, [])}
        found.append((src, len(val), [k for k, v in marks.items() if v]))
        if apply_:
            node = auth.get(src) if isinstance(auth.get(src), dict) else {}
            node["cookie"] = val
            node.setdefault("user_agent", UA)
            node["from"] = "touchfish"          # 记一笔来源，便于以后知道这串 Cookie 是哪来的
            auth[src] = node

    print("插件里配了 %d 个站的 Cookie：" % len(found))
    for src, n, marks in found:
        print("  %-9s %5d 字符   命中标记: %s" % (src, n, ", ".join(marks) or "（没命中已知标记，可能仍是好的）"))
    if missing:
        print("插件里没配：%s" % ", ".join(missing))
    if apply_:
        with open(AUTH, "w", encoding="utf-8") as f:
            json.dump(auth, f, ensure_ascii=False, indent=2)
        print("\n已写入 %s（%d 个来源；这个文件在 .gitignore 里）" % (AUTH, len(auth)))
    else:
        print("\n（试点：加 --apply 才真的写入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
