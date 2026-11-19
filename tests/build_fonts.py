# -*- coding: utf-8 -*-
"""把三套开源中文字体（宋 / 楷 / 圆）装进 reader 自己家里。

为什么必须自备字体：手机（iOS Safari）上 `Songti SC` / `Kaiti SC` / `Yuanti SC` 这些名字
对网页并不可靠——**canvas 量得出来、正文却不换**。所以发送字体文件，别指望系统。

三套都是 SIL OFL，可随包发；许可证一起放进 web/fonts/。

用法（一次性构建，产物入库）：
    python tests/build_fonts.py          # 需要 npm（会临时 npm pack 三个包）

产物：
    cloudflare-sync/web/fonts/sr-serif.woff2            宋（Noto Serif SC，简中全集 1.4 MB）
    cloudflare-sync/web/fonts/sr-round.woff2            圆（ZCOOL 庆科黄油体，简中全集 1.7 MB）
    cloudflare-sync/web/fonts/sr-kai-NN.woff2 × 97      楷（霞鹜文楷 Lite，按 unicode-range 切片）
    cloudflare-sync/web/fonts.css                       @font-face 汇总（URL 用 __FONT_BASE__ 占位）
    cloudflare-sync/web/fonts/LICENSE-*.txt             三套字体的许可证
"""
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
WEB = os.path.join(SR, "cloudflare-sync", "web")
OUT = os.path.join(WEB, "fonts")

PACKAGES = {
    "serif": "@fontsource/noto-serif-sc",
    "round": "@fontsource/zcool-qingke-huangyou",
}
KAI_PKG = "lxgw-wenkai-lite-webfont"
KAI_CSS = "package/lxgwwenkailite-regular.css"
KAI_FAMILY = "'LXGW WenKai Lite'"

CSS_HEAD = """/* 由 tests/build_fonts.py 生成，别手改。
   三套字体都是 SIL OFL，许可证在同目录 LICENSE-*.txt。
   里面的 __FONT_BASE__ 由 app.js 在运行时换成绝对地址——
   书的内容在 iframe 里，相对路径会按 iframe 自己的地址解析，所以必须给绝对 URL。 */
"""


def npm_pack(pkg, work):
    """npm pack 下来，返回 .tgz 路径。"""
    before = set(os.listdir(work))
    # Windows 上 npm 是 .cmd，subprocess 直接调会 FileNotFoundError，得经 cmd /c
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise SystemExit("找不到 npm")
    cmd = [npm, "pack", pkg, "--silent"]
    if npm.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    subprocess.run(cmd, cwd=work, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    new = [f for f in os.listdir(work) if f.endswith(".tgz") and f not in before]
    if not new:
        raise RuntimeError("npm pack %s 没产出 tgz" % pkg)
    return os.path.join(work, new[0])


def read_member(tgz, name):
    with tarfile.open(tgz) as t:
        f = t.extractfile(name)
        if f is None:
            raise KeyError(name)
        return f.read()


def extract_member(tgz, name, dest):
    data = read_member(tgz, name)
    with open(dest, "wb") as fp:
        fp.write(data)
    return len(data)


def single_subset(tgz):
    """fontsource 的 CJK 包：chinese-simplified-400.css 里只指向一个 woff2。"""
    css = read_member(tgz, "package/chinese-simplified-400.css").decode("utf-8")
    m = re.search(r"files/([^)'\"]+\.woff2)", css)
    if not m:
        raise RuntimeError("chinese-simplified-400.css 里没找到 woff2")
    return "package/files/" + m.group(1), m.group(1)


def main():
    if not shutil.which("npm"):
        raise SystemExit("找不到 npm")
    os.makedirs(OUT, exist_ok=True)
    work = tempfile.mkdtemp(prefix="srfont-")
    made = []
    css_parts = [CSS_HEAD]

    print("— 宋 / 圆：fontsource 的单文件简中全集 —")
    for key, pkg in PACKAGES.items():
        tgz = npm_pack(pkg, work)
        member, base = single_subset(tgz)
        name = "sr-%s.woff2" % key
        size = extract_member(tgz, member, os.path.join(OUT, name))
        family = {"serif": "SR Serif", "round": "SR Round"}[key]
        css_parts.append(
            "@font-face{font-family:'%s';font-style:normal;font-weight:400;"
            "font-display:swap;src:url('__FONT_BASE__%s') format('woff2')}"
            % (family, name))
        lic = os.path.join(work, "LICENSE-%s.txt" % key)
        try:
            extract_member(tgz, "package/LICENSE", lic)
            shutil.copyfile(lic, os.path.join(OUT, "LICENSE-%s.txt" % key))
        except KeyError:
            print("   （%s 包里没有 LICENSE）" % pkg)
        made.append((name, size))
        print("   %-14s %-8s %6.2f MB" % (name, key, size / 1048576.0))

    print("— 楷：霞鹜文楷 Lite，按 unicode-range 切了 97 片 —")
    tgz = npm_pack(KAI_PKG, work)
    kai_css = read_member(tgz, KAI_CSS).decode("utf-8")
    names = re.findall(r"\./files/([^)'\"]+\.woff2)", kai_css)
    if not names:
        raise RuntimeError("楷体 css 里没找到分片")
    total = 0
    for n in names:
        total += extract_member(tgz, "package/files/" + n, os.path.join(OUT, n))
    kai_css = kai_css.replace(KAI_FAMILY, "'SR Kai'")
    kai_css = re.sub(r"(\./files/)", "__FONT_BASE__", kai_css)
    css_parts.append(kai_css)
    try:
        extract_member(tgz, "package/LICENSE", os.path.join(work, "LICENSE-kai.txt"))
        shutil.copyfile(os.path.join(work, "LICENSE-kai.txt"),
                        os.path.join(OUT, "LICENSE-kai.txt"))
    except KeyError:
        print("   （楷体包里没有 LICENSE）")
    print("   %d 片，合计 %.2f MB" % (len(names), total / 1048576.0))

    with open(os.path.join(WEB, "fonts.css"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(css_parts) + "\n")

    faces = len(re.findall(r"@font-face", "\n".join(css_parts)))
    print("fonts.css：%d 条 @font-face" % faces)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
