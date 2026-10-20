# StealthReader · 摸鱼阅读器

**一张伪装成普通文本页的窗口，里面装着三样东西：你的书、社区的帖子、Anki 的卡片。**

![version](https://img.shields.io/badge/version-1.0.0-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![python](https://img.shields.io/badge/python-3.12-3776ab)
![gui](https://img.shields.io/badge/GUI-PyQt5-41cd52)

> 这不是原来那个 README。旧版写的是「PyQt5 + 本地 TXT + Legado 同步」，而现在代码里是**三个内容页 + 一整套伪装层**。本文按当前代码重写，旧版留在 git 历史里。

---

## 它是什么

一个 Windows 桌面阅读器，窗口无边框、置顶、半透明、不进任务栏（`Qt.Tool`），主体只有一个只读文本框和一个托盘图标。看起来是一小片浮在桌面上的文字，实际是一个可以在工位上读完一本书、刷完一轮社区、顺手背掉今天卡片的完整工具。

三个内容页互斥切换，**共用同一个渲染器**——所以透明度、页边距、老板键、防截屏对三者一视同仁：

| 内容页 | 入口 |
| --- | --- |
| 📖 书（本地 TXT / EPUB / Legado） | 默认页 |
| 📰 社区统一流（10 个来源 + RSS） | `Ctrl+Alt+F` |
| 🎴 隐形背诵（Anki 卡片） | `Ctrl+Alt+A` |

---

## 一、书

- **TXT / EPUB**。EPUB 用自写解析器（`zipfile` + OPF manifest + NCX/nav 两种目录），不依赖 ebooklib；TXT 自动识别 UTF-8 / GBK，并按标题行猜章节。
- **几何分页，不是按字数硬切**。每次渲染 5000 字缓冲，下一屏的起点是拿光标在**真实视口**底边量出来的——换字号、改行距、拖窗口大小都不会错位或丢字。
- **进度是全文的字符偏移**，不是页码。重启、改排版、换字体，都回到同一个字。
- **排版**：字体、字号、行距（80–250%）、段间距、页边距（单位是「字」）、首行缩进，标题永不缩进。
- **标注**：选中文字弹出五色高亮面板（黄/绿/蓝/粉/紫），可加「想法」；`Ctrl+B` 给当前页加书签；`Ctrl+L` 打开 **目录 / 书签 / 划线** 三合一，双击跳转，支持 **Markdown 导出**。数据在 `annotations.json`，按书路径分组。
- **Legado 同步**：填 `config.ip`（如 `http://127.0.0.1:1122`），走阅读 APP 的 Web 服务取书架、章节表、正文。
- **微信读书**（可选）：只做「书架 + 读到哪了 + 章节表」，**不做正文、不碰划线**——正文要绕它的保护，不做。

## 二、社区统一流

所有来源归一成 `Post / Comment / Block` 三个结构，界面层只认这三样。**加一个来源 = 加一个适配器，界面一行不改。**

| 来源 | 列表 | 正文 | 评论 | 需要登录 |
| --- | :-: | :-: | :-: | --- |
| IT之家 | ✅ | ✅ | ✅ | 免登录 |
| 虎扑 | ✅ | ✅ | ✅ | 免登录（255 个板块，可搜索） |
| 知乎 | ✅ 热榜 | ✅ | ✅ | 正文/评论要 Cookie |
| 微博 | ✅ 热搜 | ✅ | ✅ | 要 Cookie，**默认关闭** |
| NGA | ✅ | ✅ | ✅ | 要 Cookie（未登录 403） |
| 小红书 | ✅ | ✅ | ✅ | 要 Cookie（内置 `x-s` 签名） |
| linux.do | ✅ | ✅ | ✅ | 要 Cookie（含 `cf_clearance`，UA 须一致） |
| GitHub 热榜 | ✅ | ✅ README | — | 免登录 |
| Medium | ✅ 标签流 | ✅ | — | 免登录 |
| V2EX | ✅ | ✅ | ✅ | 免登录（**本机网络未实测**） |
| RSS / Atom | ✅ | ✅ | — | 自行在 `feed_rss` 里加 |

- **版式**：卡片列表 + 可折叠评论树，真三层子评论，缩进表示层级；点评论可整条收起（只留一行摘要），折叠时页面不跳。
- **知乎单独一层**：问题 → 回答（全文）→ 回答的评论；点标题看该问题下的全部回答。
- **够快**：90 秒内存直出（0 请求）、过期缓存先给后取、悬停预取、内容没变不重排、列表滚到底自动续页。
- **离线演示源**：`feed_demo/` 里的 JSON，零配置、不联网，先看效果再配 Cookie。
- 纯只读：**不发帖、不点赞、不回复**。

## 三、隐形背诵

- 只走 **AnkiConnect HTTP 接口**（信封是 `{action, version: 6, params}`，不是 JSON-RPC），**绝不直接读写 `collection.anki2`**。
- 卡片按文档排版塞进同一个窗口：`Space` / `Enter` 翻面，`1`–`4` 评分，`U` 撤销，`R` 重播音频，`Esc` 退出。
- 可选把 Anki 自己的窗口藏起来（`SW_HIDE`），退出时还原；卡在两个模式间切换也不会串键。
- 评分调用在独立线程里排队，60ms 级的 `gui*` 调用不会卡住界面。
- **隐私**：卡片正文、答案、音频文件名一律不进日志、不进异常文本、不落盘。

---

## 伪装层

| 功能 | 默认键 | 实际行为 |
| --- | --- | --- |
| **老板键** | `Esc` | 窗口整体隐藏/呼出，阅读状态完整保留；托盘图标同步。带防抖与「刚露脸」保护期，不会按住连发、不会闪一下就消失 |
| **偷瞄** | `Ctrl+E` | 窗口**不抢焦点**地浮出来，同时接管**全系统的鼠标滚轮**：滚轮翻你的书，事件被吞掉，底下的 IDE 不会跟着滚；除左键外任意键或右键/中键退出并把事件原样放行 |
| **自动挡（变色龙）** | 设置里开 | 每 500ms 采窗口左侧 5px 处一个像素当背景色（alpha 2），按亮度自动反白/反黑文字，融进任何背景 |
| **幽灵模式** | 设置里开 | 鼠标移开即全透明（`0.005`），移回恢复；高亮一并虚化 |
| **系统级防截屏** | 设置里开 | `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`，Win10 2004+：截图、录屏、会议共享里窗口不可见 |
| **安全文档** | `Ctrl+Alt+D` | 一键把书页换成一份像样的周报（内置文本，或 `safe_doc_path` 指的文件）；再按一次原样恢复 |

## 快捷键

**书页**

| 键 | 作用 |
| --- | --- |
| `→` `↓` `Space` `PageDown` | 下一屏 |
| `←` `↑` `PageUp` | 上一屏 |
| `Ctrl+B` | 当前页书签 |
| `Ctrl+L` | 目录 / 书签 / 划线 |

**社区页**

| 键 | 作用 |
| --- | --- |
| `J` `↓` / `K` `↑` | 上下移动卡片 |
| `Enter` | 进帖 |
| `Backspace` / `Esc` | 返回 |
| `Z` | 详情里回主页；列表里撤回上一次换社区/换板块 |
| `C` / `V` | 直接切下一个社区 / 下一个板块 |
| `[` `]` / `,` `.` | 社区前后翻 / 板块前后翻 |
| `E` | 直接跳到评论区顶部 |
| `R` / `O` | 重新拉取 / 打开原文 |
| `Space` `PageDown` / `PageUp` | 翻一屏 |

**卡片页**：`Space` `Enter` 翻面 · `1`–`4` 评分 · `U` 撤销 · `R` 重播音频 · `Esc` 退出

> 上面五个热键（老板键 / 偷瞄 / 安全文档 / 卡片 / 社区）都能在「设置 → 快捷键」里改，改完立刻生效。

---

## 安装与运行

### A. 有 WPS 灵犀：双击即用

双击 `run_stealthreader.cmd`。它会去 `%APPDATA%\WPS*\python-env\` 找解释器，并 `start` 拉起 `main.py`——`PyQt5` 与 `keyboard` 已经解包在 `libs/` 里，不需要 pip。

### B. 自备 Python 3.12

```bash
pip install "PyQt5==5.15.*" keyboard requests
python main.py
```

`main.py` 自己会把 `libs/` 插进 `sys.path`，所以两种方式都能起来。可选依赖：`cryptography` 或 `pycryptodome`，只有「从浏览器导入 Cookie」这一条路用得到。

> 全局热键底层是系统级键盘钩子，**建议以管理员权限运行**，否则老板键/偷瞄可能不生效。

## 配置

首次运行生成 `config.json`，改完在设置里点确定才写回。

| 键 | 说明 |
| --- | --- |
| `ip` | Legado 服务地址 |
| `opacity` `font_family` `font_size` `line_spacing` `paragraph_spacing` `page_margin_chars` `first_line_indent` | 排版 |
| `text_color` `bg_color` | `rgba(...)` 字符串 |
| `boss_key` `peek_key` `safe_key` `anki_key` `feed_key` | 五个全局热键 |
| `safe_doc_path` | 安全文档指向的文件，留空用内置周报 |
| `anki_deck` `anki_swallow_keys` `anki_conceal` `anki_api_key` | 隐形背诵 |
| `feed_sources` `feed_rss` `feed_lowkey` `feed_board_<来源>` | 启用哪些社区、订阅地址、低调显示、每个社区记住的板块 |
| `ghost_mode` `auto_mode` `antishot_mode` | 三个伪装开关 |
| `last_local_file` `last_local_pos` | 上次的书与阅读位置 |

**凭据**放在 `feed_auth.json`（已在 `.gitignore` 里）：

```json
{ "zhihu": { "cookie": "...", "user_agent": "..." } }
```

`feed_cookies.py` 能**只读**地从本机浏览器把 Cookie 捞出来（Firefox 明文；Chrome/Edge/Brave/Vivaldi 走 DPAPI + AES-256-GCM），只取 `SOURCE_DOMAINS` 里那几个域名，结果只写这一个文件。

## 模块

| 文件 | 职责 |
| --- | --- |
| `main.py` | 窗口、渲染、分页、伪装层、三个模式的状态机 |
| `feed.py` | 社区数据模型、排版、导航、HTML → Block |
| `feed_net.py` | 取数与缓存：超时、按域限速、磁盘缓存、图片落地 |
| `feed_sources.py` | 每个站一个适配器 + 混流 |
| `feed_more.py` | 订阅式来源（RSS / Medium / GitHub 热榜） |
| `feed_cookies.py` | 从本机浏览器只读导入 Cookie |
| `xhs_sign.py` | 小红书 `x-s` / `x-t` / `x-s-common` 签名（纯标准库） |
| `anki_client.py` | AnkiConnect 客户端（含错误分类与绕代理） |
| `weread.py` | 微信读书书架与进度 |
| `hupu_boards.py` | 虎扑板块表（生成物，别手改） |

## 测试

`tests/` 里是**零网络探针**，不是 pytest：

- `probe_sources.py` — 把 `requests` 打成"一调就抛"，适配器只吃 `tests/fixtures/` 里的**真实响应裁剪**
- `probe_ui.py` — Qt offscreen 跑窗口逻辑
- `probe_weread.py` / `check_auth.py` — 微信读书与各站 Cookie 自检
- `probe_live.py` — 真实网络冒烟（要网，也会打人家服务器，自己掂量）

夹具覆盖 IT之家、知乎、微博、小红书、虎扑、NGA（含两种 403）、Discourse、Cloudflare 567 拦截页。`record_fixtures.py` 可重录。

## 已知限制

不美化，按实际情况写：

- **仅 Windows**。防截屏、窗口归属、钩子、`SW_HIDE` 全是 Win32 调用。
- **纯只读**。社区流不发帖、不点赞、不回复；小红书只搬读评论所需的签名，写操作那条路没搬。
- **V2EX 未实测**：适配器按官方文档写、有夹具验证，但作者那台机器到 V2EX 的 443 一直超时。
- **Chiphell 放弃**：撞上非标准的 HTTP 567 JS 挑战，只留了实测结论和一个夹具。
- **SST 博客未接入**：代码在 `feed_more.py` 里，没进 `build_real_sources`。
- **微信读书只到书架**：正文/划线/书签都没做（划线拿到的只是 range，要正文才能还原）。
- **Cookie 是明文**：存在 `feed_auth.json`（已 gitignore）。设计文档里写过的 DPAPI 加密，代码没有实现——别提交、别分享这个文件。
- **`libs/` 不入库**（约 140MB）。从源码跑请自备 PyQt5 与 keyboard。

## 免责

抓的都是公开页面，只读，不动别人账号；Cookie 是你自己的登录态，泄露等于交出账号，请自己看住 `feed_auth.json`。至于上班时间看什么、能不能在公司电脑上装，这是你和你公司之间的约定，本工具不替你做判断。

## 许可

仓库目前**没有 LICENSE 文件**。在法律意义上默认保留所有权利；要开源给他人用，请先补一份许可证。
