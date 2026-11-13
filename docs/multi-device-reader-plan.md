# 多端浏览器阅读页面：方案

目标：手机 / Kindle / 电脑都能用一个网页读书，字体排版能调，**一次只加载正在读的那一本**，
进度、划线、排版设置跟着现有的同步系统走（和桌面端共用同一套 state 对象和同一个 token）。

现状可以复用的东西（都不用重做）：

| 已有 | 位置 | 对新页面意味着什么 |
| --- | --- | --- |
| 书文件 | R2 `v1/blob/<sha256>` + `customMetadata.name` | 网页端按 sha 直接取一本，不用另存一份 |
| 书清单 | `GET /v1/blobs` | 里面缺"进度/书名"的汇总，补一个 `/v1/shelf` |
| 四个状态对象 | `v1/state/{progress,annotations,stats,settings}.json` + ETag/`If-Match` | 网页端读写进度、排版设置就是打这几个接口 |
| 书身份 | `uid = sha256[:16]` | 三端认的是同一个 id |
| 进度语义 | 桌面端 `pos` = 抽出来的纯文本**字符偏移**，`total` = 总字符数 | 网页端只要也能算出"第几个字符"，三端就天然对齐 |
| 鉴权 | `Authorization: Bearer <token>` | 网页端换成同源 Cookie，一次性登录 |

## 1. 选型：foliate-js（MIT）

- 纯 JS、**原生 ES modules、没有构建步骤**，直接把文件拷进 `web/` 就能跑；
- 支持 EPUB / MOBI / KF8(.azw3) / FB2 / CBZ，PDF 实验性——你书架里那些 z-library 的 epub 和 Kindle 的 azw3 都吃；
- **不要求把整本书读进内存**（配 zip.js 走 HTTP Range），符合"单本加载"；
- 说明里写着 "does not care about older browsers"：目标是最新 WebKitGTK / Firefox / Chromium。

代价与注意点（都是可接受的）：

- API 还没稳定（作者自己说的），所以要**固定一个 commit**：以 git submodule 引（作者建议的方式）；
  不想在 Windows 上折腾 submodule 的话，就拷进 `web/vendor/foliate-js/`，旁边放一份 `VENDOR.md` 记 commit 和许可证；
- 它明确要求页面加 CSP `script-src 'self'`：EPUB 里可以带 JS，而内容是以 `blob:` 同源的，不加 CSP 等于让书里的脚本在我们域名下跑。**这条是硬要求**；
- 衬线字体：CJK 字体动辄 5–10 MB，网页端不自己打包，用系统字体（`-apple-system` / `Songti SC` / `Noto Serif CJK` / `SimSun` 这套 fallback），Kindle 用它自带的。

另一个候选是 epub.js（只 EPUB、更老、分页实现被 foliate-js 作者点过几处问题），不选。

## 2. 三端怎么读

| 端 | 入口 | 渲染 | 说明 |
| --- | --- | --- | --- |
| 电脑（Chrome/Edge/Safari） | `/r/` | foliate-js 分页 + 双栏 | 键盘 `←/→/空格`，目录侧栏 |
| 手机（iOS Safari / Android Chrome） | `/r/` | 同一套，单栏分页/滚动可切 | 全屏、安全区、隐藏地址栏；PWA 二期 |
| Kindle（自带浏览器） | `/k/<uid>?p=N` | **服务端出整页 HTML**，没有 JS | Kindle 的浏览器太老，不赌它跑现代 JS |

Kindle 这条路的关键：**分页不用 JS 算**。

- 桌面端（或网页端）把书正文按章拆成 `v1/lite/<uid>/<n>.json`（段落数组），再存一份 `index.json`（章标题、起始字符偏移、字符数）；
- Worker 收到 `/k/<uid>?p=37`：算出这是第几章的第几"屏"（按"每屏字数"切段落），取那一章 JSON，套模板吐一页纯 HTML，带"上一页/下一页"两个链接；
- 翻页 = 一个 GET，一页约 40–80 KB；`pos` 就是这一屏第一个字在全书里的偏移，**进度天然精确**；
- 每屏字数可以在页面上用链接切（600 / 800 / 1000 字），因为 Kindle 上量不到真实视口。

所以 lite 这条路不只是 Kindle：**任何老浏览器/低端机都能走**，也就是"兼容模式"。

## 3. 一次只加载一本（硬性）

- `GET /v1/shelf` 只回书名、uid、sha、大小、进度、总字数——**不含正文、不含所有书的字节**；
- 打开一本才 `GET /v1/blob/<sha>`（二期给 Worker 加 `Range`，大书边读边开）；
- Kindle 更省：一次只取一章、只渲染一屏；
- 手机上"继续读"只看最后一本，其余在书架上按需加载。

## 4. 进度坐标：字符偏移是唯一真值

- 桌面端早就在用 `pos`（字符偏移）/ `total`；网页端把当前位置换算成"从全书第一个字起了多少个字符"：
  - SPA：foliate-js 的 `text-walker` 已经能把 Range 对应到文本，累积前面的章节字符数即可（章节字符数来自 `index.json`，与桌面端同一份）；
  - lite：`pos` 就是分页表算出来的偏移，天生一致；
- 同时把 foliate-js 的 CFI 存进 `cfi` 字段（只给网页端自用，桌面端忽略）：同一本书在网页端内部跳转更准，`pos` 仍然负责跨端；
- 两端 `total` 不一致（抽取规则不同）时，用 `pos/total` 的比例兜底，并把差异写日志——不静默。

## 5. 写回：只改自己那一行，**不把 Python 的合并逻辑翻成 JS**

桌面端的 `merge_progress` / `merge_settings` 有明确的时钟规则（`at` 新的赢、`total` 取大、`hidden` 取或；设置按 key 的 `clock` 逐键比）。网页端**不重写这套合并**，只做"定点修改"：

1. `GET /v1/state/progress`（拿 ETag）；
2. 只把**正在读的这本书那一行**替换成 `{title, pos, total, at: now, dev: "web-ios"}`，其他行原样保留；
3. `PUT` 带 `If-Match`；412 就重来一遍（最多 3 次）。

为什么这样是对的：桌面端的规则里，这一行 `at` 最新就一定赢，所以"改自己这一行 + 最新时间戳"与合并结果等价；而其他行我们连碰都不碰，不存在覆盖别人进度的可能。

设置同理：只改用户刚动的那几个键，并给每个键写上 `clock[key] = now`（桌面端逐键比时钟，没这个时钟就会被当成旧值压掉）。

这条也决定了实现顺序：**一期只写进度 + 排版设置，不写划线**；划线/书签（要用 overlayer 和墓碑语义）放三期，届时同样按"只增不删"的定点修改来做。

## 6. 排版设置：和桌面端同一批键

`settings` 里已有的键，网页端直接读、直接写：

| config 键 | 网页 CSS |
| --- | --- |
| `font_size` | `font-size: {n}px` |
| `font_family` | `font-family`（给一套系统字体映射，缺字体回退） |
| `line_spacing` | `line-height` |
| `paragraph_spacing` | `p { margin-bottom }` |
| `page_margin_chars` | 容器左右 `padding`（按字号换算成 px） |
| `first_line_indent` | `p { text-indent }` |
| `bg_color` / `text_color` | 主题色（另加"护眼/夜间"两档预设写进这两个键） |

面板就是 7 个控件 + 3 个预设；改完立刻 `PUT`，桌面端下一轮同步就跟着变（桌面端本来就会读这些键）。

## 7. 接口（在现有 Worker 上加，不新开服务）

```
GET  /v1/shelf                     书架：[{uid, sha, name, size, title, pos, total, at}]（不含正文）
GET  /v1/blob/<sha64>              现有；二期加 Range
GET  /v1/lite/<uid>/index.json     章节目录 + 字符偏移（桌面端/网页端生成后上传）
GET  /v1/lite/<uid>/<n>.json       第 n 章段落（lite 用）
GET  /r/…                          静态壳（Workers Assets，同域，无 CORS）
GET  /k/<uid>?p=N                  服务端渲染的一屏 HTML（无 JS）
POST /k/progress                   表单提交 pos/total → 302 回原页
POST /v1/login                     token → Set-Cookie（HttpOnly, Secure, SameSite=Lax）
```

- 同域（`sync.noteklip.org/r/`）省掉 CORS、第二个证书、第二个域名；
- Cookie 登录后，`/r/` 和 `/k/` 都用 Cookie 鉴权；`/v1/*` 继续同时接受 `Authorization`（桌面端不受影响）；
- 静态资源用 wrangler 的 `assets`（当前 wrangler 4.161 支持），`/v1/*` 仍然走 Worker。

## 8. 流量预算（按之前实测的 ~160 KB/s）

| 动作 | 字节 | 首次耗时 |
| --- | --- | --- |
| 打开 `/r/` 壳（含 foliate-js + zip.js） | 估 250–400 KB | 2–3 秒 |
| 打开一本书（2 MB epub） | 2 MB | ~13 秒（之后 Cache API 命中） |
| 手机"继续读" | 壳（缓存命中）+ 那本书 | ~0 + 13 秒 |
| Kindle 翻一屏 | 40–80 KB | < 1 秒 |

壳的体积要实测，超 400 KB 就按需动态 import（foliate-js 本来就是模块化的）。

## 9. 分期

**一期（PC + 手机能读、进度和排版跟着同步）**
1. Worker：`/v1/shelf`、`/v1/login`、Cookie 鉴权、CSP、`assets` 静态托管；
2. `web/`：foliate-js（submodule 固定 commit）+ 书架 + 阅读页 + 进度写回 + 排版面板；
3. 探针：`tests/probe_web.py`（无浏览器也能测的 HTTP 层：shelf 形状、登录、CSP 头、进度 PUT 的 412 重试）+ 一个 Playwright 冒烟脚本（可选，跑不动就人工验收清单）。

**二期（Kindle）**
1. 桌面端：`为多端准备` 把书拆章上传 `v1/lite/…`（复用现有 EPUB 解析，一本书几秒）；
2. Worker：`/k/` 分页渲染 + 表单回写进度；
3. 真机验收（PW / 老 Kindle 各一台），字号/每屏字数两档可调。

**三期（划线 + PWA + 手机传书）**
1. overlayer 划线与书签（只增不删的定点修改 + 现有墓碑语义）；
2. Service Worker 缓存壳和最近 3 本；
3. 网页端直接传书（Worker 转 R2）。

## 10. 风险与要先验的

| 风险 | 怎么处理 |
| --- | --- |
| Kindle 浏览器版本差异大 | 二期先拿真机试 `/k/`；不行就退到"桌面端预渲染成 HTML 存 R2，Worker 只做静态转发" |
| iOS Safari 的 CSS 分栏分页在滚动容器里有老 bug | 用 foliate-js 的 paginator（它专门处理过），手机默认给"滚动"模式兜底 |
| 中文字体缺失 | 只用系统字体；不打包 CJK 字体 |
| EPUB 里带脚本 | CSP `script-src 'self'`（foliate-js 作者的硬要求），并在 Worker 给 `/r/` 加 `X-Content-Type-Options: nosniff` |
| 网页端写坏进度 | 只改自己那一行 + `If-Match` + 3 次重试。语义与桌面端**完全一致**：`at` 最新的一行赢（所以回翻也会把进度往回写——这是现有行为，网页端不自作主张改成"单调不减"，否则两端会各说各话） |

## 11. 要拍板的三件事

1. **引擎**：foliate-js（MIT，EPUB/MOBI/AZW3 都吃，无构建）——默认就它；
2. **Kindle**：走"服务端出 HTML"（二期），不让 Kindle 跑现代 JS；
3. **入口**：同域 `sync.noteklip.org/r/`（省 CORS 和证书）。

三件都按默认走的话，一期可以直接开工。
