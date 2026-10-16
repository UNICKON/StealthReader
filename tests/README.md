# 探针（社区统一流的验收）

这一批功能的验收不是"我觉得能跑"，而是三个能重跑的探针。改动社区那一层之后，
至少把前两个跑一遍——它们是零网络的，几秒钟出结果，不碰网络也不碰书架。

```powershell
$py = "$env:APPDATA\WPS 灵犀\python-env\python.exe"     # 就是跑 main.py 的那个解释器

& $py -X utf8 tests\probe_sources.py     # 来源层逻辑（零网络）
& $py -X utf8 tests\probe_ui.py          # 界面集成（零网络，离屏 Qt 走完整窗）
& $py -X utf8 tests\probe_live.py        # 真机联网冒烟（会真的发请求，跑完自清缓存）

& $py -X utf8 tests\check_auth.py        # 凭据自检：我贴的 Cookie 到底管不管用（联网）
& $py -X utf8 tests\check_auth.py --all  # 连没配 Cookie 的也查一遍（看基线是什么样）

& $py -X utf8 tests\import_touchfish_cookies.py --apply   # 从 VS Code 摸鱼插件搬 Cookie（只读它、只写 feed_auth.json）
```

## 三个探针各管什么

| 探针 | 网络 | 管什么 |
|---|---|---|
| `probe_sources.py` | **零** | 每个来源的 列表 / 正文 / 评论 映射、统计单位、缓存与断网回落、混流分额与转发、凭据管线、状态码→人话的映射、结构不变量 |
| `probe_ui.py` | **零** | 真适配器 + 替身 Http 走完整窗：混流席位、`[` `]` 切来源、`,` `.` 切栏目、详情、`O` 开原帖、退出不动书的状态、两个"结果晚到"的竞态 |
| `probe_live.py` | 真 | 真站能不能拿到、缓存第二次是否 0 秒、混流是否并行、整窗真拉一遍、**配了 Cookie 的站正文/评论通不通** |
| `check_auth.py` | 真 | 贴了 Cookie 之后一个站一个站地验证：HTTP 码、返回体顶层键、一句人话结论 |

`import_touchfish_cookies.py` 是给"我自己贴 Cookie"这条路减负的：VS Code 摸鱼插件
（`ylw.touchfish`）已经把各站 Cookie 明文放在 `%APPDATA%\Code\User\settings.json`，
这个脚本**只读它、只写 `feed_auth.json`**（gitignored），并且**不打印 Cookie 内容**——只报
"这个站有没有、多少字符、命中了哪个标记"。加 `--apply` 才真的写。

**"零网络"是可以验证的，不是靠自觉**：`probe_sources.py` / `probe_ui.py` 开头把
`requests.get / post / Session.get` 全换成抛异常，而所有适配器拿到的是一个读夹具的替身 Http。
所以只要探针是绿的，就说明这次一个网络包都没发。

`probe_ui.py` 离屏跑（`QT_QPA_PLATFORM=offscreen`），会短暂开一个窗口对象然后销毁；
它把 `save_annotations`、`log_event`、全局热键、`os.startfile` 全换成桩，
并且停掉进度保存定时器——**不会写 config.json，不会动阅读进度，不会真的开浏览器**。

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
| `weread_unauth_shelf.json` | 微信读书书架接口**没登录时**的真响应（`errCode -2010`） |

后五个是**"失败"的真响应**，专门用来验"连不上/没登录时页面说的是实话"——这类路径最容易
被写成"假装成功"，所以更要拿真响应盯住。

**带 Cookie 录的夹具一律裁过**（`_trim_answer` / `_trim_comments`）：知乎的正文和评论是**别人写的
话**，不是我的代码产物，所以只留"够验形状"的一小段（第一段正文、前 3 条评论各前 40 字），
长文和头像地址、用户 id 都不进仓库。裁掉的部分不影响验收——要验的是"字段取对了没有"。

**小红书那两份是"状态块"，不是网页**：它的内容不在接口里，在页面的
`window.__INITIAL_STATE__` 里，所以录的是**裁过的状态 JSON**，探针再照真页面的写法把它包成
`<script>window.__INITIAL_STATE__=…</script>` 喂给适配器（页面壳是我们包的，状态本身是真响应）。
`xhs_note_state.json` 里的 `comments.list` 是**空的**——不是忘了裁，是录的时候**连看 10 条笔记
都没带上评论**（小红书的评论是滚动加载的，评论接口又要前端签名，实测 406）。既然拿不到真夹具，
适配器里的 `comments()` 就**直接返回空**、并在正文里写明为什么，而不是留一层"猜出来的解析"。

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
