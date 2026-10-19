# 探针（社区统一流的验收）

这一批功能的验收不是"我觉得能跑"，而是三个能重跑的探针。改动社区那一层之后，
至少把前两个跑一遍——它们是零网络的，几秒钟出结果，不碰网络也不碰书架。

```powershell
$py = "$env:APPDATA\WPS 灵犀\python-env\python.exe"     # 就是跑 main.py 的那个解释器

& $py -X utf8 tests\probe_sources.py     # 来源层逻辑（零网络）
& $py -X utf8 tests\probe_ui.py          # 界面集成（零网络，离屏 Qt 走完整窗）
& $py -X utf8 tests\probe_weread.py      # 微信读书书架那一层（零网络）
& $py -X utf8 tests\probe_live.py        # 真机联网冒烟（会真的发请求，跑完自清缓存）

& $py -X utf8 tests\check_auth.py        # 凭据自检：我贴的 Cookie 到底管不管用（联网）
& $py -X utf8 tests\check_auth.py --all  # 连没配 Cookie 的也查一遍（看基线是什么样）

& $py -X utf8 tests\import_touchfish_cookies.py --apply   # 从 VS Code 摸鱼插件搬 Cookie（只读它、只写 feed_auth.json）
```

## 四个探针各管什么

| 探针 | 网络 | 管什么 |
|---|---|---|
| `probe_sources.py` | **零** | 每个来源的 列表 / 正文 / 评论 映射、统计单位、缓存与断网回落、混流分额与转发、凭据管线、状态码→人话的映射、结构不变量；**NGA 的编码判据（自称 gbk 实为 UTF-8 / 真 GBK / 结构里嵌乱码 / 正常中文不误判）**；**虎扑的版面表、列表 HTML、`__NEXT_DATA__` 正文与回复、翻页拼地址**；知乎三栏、问题→回答→评论三层（回答摆全文、回答底下的评论当子节点、表头写"N 条回答"）；**小红书的 `x-s` 签名（签名明文 / `mns0301_` 串 / `b1` / `x-s-common` / CRC / 自定义 base64，全部钉成和摸鱼插件 JS 版逐字节一致的固定值）、评论解析**；**版式（标题只比正文大 1px、不画分隔线、评论树每层缩进 16px 且子回复是真子节点、字符画确实没了、标题截断与定位文本同源、空评论不补占位句、来源代码里不再有说明句）**；**刷新不吃缓存（`set_fresh` 只影响本线程，且来源内部的内存缓存也要让路）** |
| `probe_ui.py` | **零** | 真适配器 + 替身 Http 走完整窗：混流席位、`[` `]` 切来源（真 QKeyEvent）、`,` `.` 切栏目、详情、`O` 开原帖、退出不动书的状态、两个"结果晚到"的竞态、按像素点选与落点、进出社区页的排版隔离、社区页字体、**换社区/换板块（菜单里能看见、按来源记忆、后台拉板块表→可搜索的选择器）**、**老板键唤出（刚唤出的收起会被忽略、过临界期照常收起、看得见就不挪窗口、偷瞄残留钩子会被卸掉）**、**按 `R` 刷新：列表和详情都带 `fresh=True`、详情不会被刷新打回列表**、微信读书书架窗口 |
| `probe_weread.py` | **零** | 微信读书那一层：书架与进度怎么合、章节表怎么排、`errCode` 怎么说人话、没 Cookie 时一个包都不发、POST 缓存不串书；还有两条钉住"**不做正文**"的断言 |
| `probe_live.py` | 真 | 真站能不能拿到、缓存第二次是否 0 秒、混流是否并行、整窗真拉一遍、**配了 Cookie 的站正文/评论通不通**、微信读书书架 |
| `check_auth.py` | 真 | 贴了 Cookie 之后一个站一个站地验证：HTTP 码、返回体顶层键、一句人话结论 |

微信读书的正文**不在**这四个探针的覆盖里，而且不是"还没做"：正文要绕过它自己那套签名 +
校验（`e_*`/`t_*`），那是它专门防程序取正文的保护措施，不做。原因与断言写在
`weread.py` 开头和 `probe_weread.py` 第 9 节。

`import_touchfish_cookies.py` 是给"我自己贴 Cookie"这条路减负的：VS Code 摸鱼插件
（`ylw.touchfish`）已经把各站 Cookie 明文放在 `%APPDATA%\Code\User\settings.json`，
这个脚本**只读它、只写 `feed_auth.json`**（gitignored），并且**不打印 Cookie 内容**——只报
"这个站有没有、多少字符、命中了哪个标记"。加 `--apply` 才真的写。

**"零网络"是可以验证的，不是靠自觉**：`probe_sources.py` / `probe_ui.py` / `probe_weread.py`
开头把 `requests.get / post / Session.get` 全换成抛异常，而所有适配器拿到的是一个读夹具的替身 Http。
所以只要探针是绿的，就说明这次一个网络包都没发。

`probe_ui.py` 离屏跑（`QT_QPA_PLATFORM=offscreen`），会短暂开一个窗口对象然后销毁；
它把 `save_annotations`、`log_event`、全局热键、`os.startfile` 全换成桩，
并且停掉进度保存定时器。**但光靠这些桩不够**：本轮真出现过"探针把真人 config.json 改坏"（社区字体被写成 Helvetica、多出个 `feed_board_boardsrc`、阅读进度被清成 0），
有些路绕过了替身直接落了盘。所以三个探针现在**开跑前备份、`atexit` 原样写回**，
**速度（内存先画 / 预取选中那条 / 内容没变不重排 / 过期缓存先给后取）**、跑完 `config.json` 一个字节都不变（探针自己比 SHA-256）；`probe_ui.py` 还额外把
模块级 `save_config` 也堵上。

`check_auth.py` 只读、不发帖不点赞；**带 Cookie 拿到的响应只写到
`%TEMP%\sr_auth_probe\`，绝不写进仓库**（那里面有你的用户名、头像地址这些东西）。
要拿它当夹具，先自己看一遍再手动搬过来。

## 夹具（`fixtures/`）

夹具是**真响应**，由 `tests/record_fixtures.py` 录下来（那个脚本故意联网，只在需要更新夹具时跑）：

```powershell
& $py -X utf8 tests\record_fixtures.py            # 全部重录
& $py -X utf8 tests\record_fixtures.py zhihu      # 只录某一个
```

| 文件 | 来自 |
|---|---|
| `ithome_hot.json` / `ithome_newslist.json` / `ithome_detail.json` | IT之家移动端接口 / `api/newslist/news` / `newscontent` |
| `zhihu_hot_list.json` | 知乎 `api.zhihu.com/topstory/hot-list` |
| `weibo_hotsearch.json` | 微博 `weibo.com/ajax/side/hotSearch` |
| `discourse_latest.json` / `discourse_topic.json` | **meta.discourse.org**（官方 Discourse 站） |
| `nga_unauth_403.json` | NGA **没登录时**的真响应（GBK，`1:未登录`） |
| `nga_list.json` / `nga_read.json` | NGA **带 Cookie** 的列表与正文，**裁过**：列表留 6 条（含 1 条它自己的错误提示）、正文留前 3 楼各 220 字，用户表只留 `uid`+`username` |
| `nga_old_thread_403.json` | NGA **老帖**的真 403（GBK，原样）：`5:帖子发布或回复时间超过限制` |
| `xhs_explore_state.json` | 小红书探索页 SSR 状态，**裁过**：只留 3 条笔记 + 适配器用到的字段（`id`/`xsecToken`/`noteCard`） |
| `xhs_note_state.json` | 小红书笔记详情的 SSR 状态，**裁过**：正文前 400 字、图前 2 张、去掉 `ipLocation`（评论那份录的时候就是空的，见下） |
| `zhihu_answer.json` / `zhihu_comments.json` | 知乎**带 Cookie** 拿到的正文/评论，**裁过**：只留第一段正文、前 3 条评论各前 40 字 |
| `linuxdo_cf.html` | linux.do 的 Cloudflare `Just a moment...` 拦截页 |
| `chiphell_567.html` | Chiphell 的非标准 567 防爬占位页 |
| `zhihu_unauth_answers.json` | 知乎正文接口**没登录时**的真响应（`code 40362` 风控） |
| `hupu_list.html` | 虎扑版面页 `bbs.hupu.com/bxj`（**免登录**），**裁过**：只留前 12 条 `<li class="bbs-sl-web-post-body">`，外壳换成最小 HTML |
| `hupu_thread.html` | 虎扑帖子页，**裁过**：`__NEXT_DATA__` 只留 `detail.thread`（正文去掉 `data-origin`）和前 6 条回复；挑的是**有文字正文**的那篇（列表里很多是纯图/视频帖，正文一个字都没有） |
| `weread_unauth_shelf.json` | 微信读书书架接口**没登录时**的真响应（`errCode -2010`） |
| `weread_shelf.json` | 微信读书书架，**裁过**：只留 3 本 + 适配器用到的字段（书名/作者/封面/`deepLink`/进度） |
| `weread_chapters.json` | 微信读书章节表，**裁过**：前 3 章 + **当前读到的那一章**（先录的前 3 章里根本没有它，"目录里 ▶ 标出读到哪章"这条就没法验） |
| `weread_progress.json` | 微信读书进度，**裁过**：`summary` 只留前 60 字（那是**书里的原句**，跟知乎正文一个规矩） |

带 `unauth` / `_403` / `_cf` / `_567` 的那几份是**"失败"的真响应**，专门用来验
"连不上/没登录时页面说的是实话"——这类路径最容易被写成"假装成功"，所以更要拿真响应盯住。

**带 Cookie 录的夹具一律裁过**（`_trim_answer` / `_trim_comments`）：知乎的正文和评论是**别人写的
话**，不是我的代码产物，所以只留"够验形状"的一小段（第一段正文、前 3 条评论各前 40 字），
长文和头像地址、用户 id 都不进仓库。裁掉的部分不影响验收——要验的是"字段取对了没有"。

**小红书那两份是"状态块"，不是网页**：它的内容不在接口里，在页面的
`window.__INITIAL_STATE__` 里，所以录的是**裁过的状态 JSON**，探针再照真页面的写法把它包成
`<script>window.__INITIAL_STATE__=…</script>` 喂给适配器（页面壳是我们包的，状态本身是真响应）。
`xhs_note_state.json` 里的 `comments.list` 是**空的**——不是忘了裁，是录那份状态的时候
页面本身就没把评论渲进状态块（小红书评论是另走接口滚动加载的）。
评论这条路的**签名**后来补上了（`xhs_sign.py`，照 VS Code 摸鱼插件 TouchFish 的纯 JS 实现搬的，
逐字节比对过），当初只发内层 `mns0301_` 串所以一律 **406**；**包上 `"XYS_"` 那层信封之后
就通了**（真机实测 200 + 10 条评论带作者/赞/子评论）。所以现在 §17 除了钉签名各层的向量，
还钉了信封本身；**这条链现在是通的，别再退回只发内层**。

`discourse_*` 那一行的来源要看清楚：**不是 linux.do**。linux.do 前面挂着 Cloudflare 盾，
本机录不到（403/超时），而 Discourse 适配器（回复树、最后回复时间、广告帖兜底）没有夹具就等于
没人盯，所以退回官方的 meta.discourse.org 录——两边是同一种 Discourse，结构一致。
linux.do 特有的那部分（`cf_clearance` + UA 必须一致）不在这个夹具的覆盖范围里，
真机能不能过只能靠 `check_auth.py` 和实际使用时的页脚反馈。

录不到的站**不写空文件**：那个站对应的探针章节自己会 `skip`（并在结果里报"跳过 N 项"），
所以夹具目录里"缺哪个文件"本身就是一条信息。

**手写形状的例外**：探针里有几条明确标着"（手写形状）"的检查——那些是"按公开的接口约定手写
一小段响应"，用来验 NGA 的 `__T`/`__R` 收表、BBCode 清理、以及"Cookie 管用时知乎正文的解析"
这类**我自己的解析逻辑**。它们只证明"我的代码自洽"，**不证明"代码符合那个站"**——
真正的证明要等 `check_auth.py` 把真响应存下来，再按真形状收紧。
