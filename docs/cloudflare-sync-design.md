# 用 Cloudflare 做存储同步：设计

> 2026-11-12。这份是设计，不是实现计划：**没有一行产品代码**，先把形状定下来。
> 技术选型（我替你定的）：服务端 Cloudflare Worker + R2 + 一个自定义域名；客户端 Python 3.12 纯标准库
> （`urllib` / `ssl` / `threading` / `hashlib`），不引第三方；Worker 侧纯 JS，不引框架。

## 1. 一句话

阅读数据留在本地照旧，**多出一份云端副本**：换机器、重装、误删都能捞回来；两台机器轮流读同一本书时，
进度和笔记自动并到一起。同步是后台的一件事，网络不通就等于没这功能，绝不挡你翻页。

## 2. 现在有什么（要同步的四类东西）

| 数据 | 落盘位置 | 现在的大小 | 结构要点 |
|---|---|---|---|
| 设置 + 书架进度 | `config.json` | 8.4 KB | `local_book_positions` / `_totals` / `_times` / `_hidden`，键是 `normcase(绝对路径)` |
| 划线 / 书签 | `annotations.json` | 4.4 KB | `{书key: {highlights: [...], bookmarks: [...]}}`，每条有 `id`（`h<毫秒>` / `b<毫秒>`） |
| 阅读账本 | `reading_stats.json` | 5.0 KB | `sessions` 是唯一真相，`days` / `books` 可由 `consolidate()` 重算 |
| 书文件本身 | 书架目录（这里 `C:\Users\KSO\Downloads`） | 9 本 ≈ 19 MB | epub，最大的 8.1 MB |

### 三个必须先解决的坑

1. **所有键都是本机绝对路径**。`normcase(abspath)` 换一台机器就全废（盘符、用户名、目录都不同）。
   直接拿它当同步键，第二台机器要么对不上、要么把 10 本书的进度全变成"新书"。
2. **两份 json 是"整份重写"**。同时改 → 后写的把先写的盖掉，必须做合并而不是覆盖。
3. **不能碰坏现有账本**。你已经明确过一次：碎场要并不要删。同步只允许"并"，不允许"以远端为准覆盖本地"。

## 3. 目标 / 非目标 / 反目标

**目标**
- 换机器 / 重装：进度、划线书签、阅读账本回来（书文件可选）。
- 两台机器轮流读：同一本书的进度并成一条（谁后读算谁），笔记两边都在。
- 手机热点的弱网下不挡界面、不丢数据、断了自动续。
- 出问题能退：关掉同步 = 回到今天的样子，一个字节都不少。

**非目标**
- 不做账号体系 / 多用户 / 分享。就你一个人、一个 token。
- 不做实时协作，不追求秒级一致（分钟级足够）。
- 不做"在网页上看书"（书不在云上当阅读器用，只当备份）。

**反目标（做到这些就算失败）**
- 任何一次同步导致本地数据变少或变旧。
- 界面因为等网络卡住半秒以上。
- 需要你手工处理冲突文件（"请选择保留哪个版本"这种对话框不算解决方案，只能当兜底）。
- 离线时反复弹窗报错。

## 4. 总体形状

```
        ┌──────────────── 本机（PyQt5 主进程）────────────────┐
        │ 读/写照旧：config.json / annotations.json /          │
        │           reading_stats.json                        │
        │        │ 变更（3 秒防抖）        ▲ 合并结果写回       │
        │        ▼                        │                   │
        │   sync_engine.py（后台线程，标准库）                  │
        │   ├─ 打包：把三份数据化成「同步对象」                  │
        │   ├─ 合并：本地 ↔ 远端（纯函数，可单测）              │
        │   └─ 队列：离线堆着，退避重试                        │
        └────────┬───────────────────────────────────────────┘
                 │ HTTPS + Bearer token，只认 base_url
                 ▼
        ┌──────────── Cloudflare Worker（鉴权 + 条件写）────────┐
        │  GET /v1/ping          ← 自检                          │
        │  GET /v1/manifest      ← 各对象的 rev(=ETag)            │
        │  GET/PUT /v1/state/{kind}   If-Match → 412 冲突        │
        │  PUT /v1/blob/check + /v1/blob/{sha256}  ← 书文件(可选) │
        └────────┬───────────────────────────────────────────────┘
                 ▼
        ┌──────────── R2 bucket（对象存储）──────────────────────┐
        │  v1/state/progress.json     进度（按 book_uid）        │
        │  v1/state/annotations.json  划线/书签（含墓碑）        │
        │  v1/state/stats.json        阅读账本（sessions）       │
        │  v1/state/settings.json     设置白名单                 │
        │  v1/state/history/{kind}/{ts}.json  最近 5 个旧版本    │
        │  v1/blob/{sha256}           书文件（内容寻址，可选）    │
        └───────────────────────────────────────────────────────┘
```

**一个关键决定**：客户端只认 `base_url` + `token`，不认识 Cloudflare。哪天 CF 不通了，
把 `base_url` 换成自己的服务器（同一套 HTTP 语义）就行，客户端不用改。CF 只是"第一个后端"。

## 5. 数据模型

### 5.1 书身份：从路径改成内容 UID（迁移的第一步）

新增 `book_uid` = 文件内容 `sha256` 的前 16 个十六进制字符。只在**要同步的书**上算，
结果缓存在 `sync_state.json` 的 `uids` 里（**不放 config.json**：那份文件每翻一页就被内存副本整份重写，
放进去会被擦掉，见 §5.3）：

```json
"uids": {
  "c:\\users\\kso\\downloads\\雨 (黄锦树)...epub": {"uid": "3f9a1c2b7d4e5f60", "size": 366182, "mtime": 1762...}
}
```

规则：
- 缓存命中条件 `size` + `mtime` 都没变（改一个字节就重算）；算一次 8 MB 的 epub 约 20 ms，9 本一次性 < 0.2 s，
  放在后台线程里做，界面不等。
- 同一本书在两台机器上路径不同但内容相同 → UID 相同 → 进度/笔记天然对上。
- 老数据迁移：第一次启用同步时，把已有的 `local_book_positions` / `annotations` 按本机路径算出 UID，
  生成"UID 视图"再上传；本机原有的路径键**不动**。
- **文件已经不在的书**（现场就有：6 条划线挂在 `C:\Users\KSO\Desktop\当下的力量….epub`，那个文件已经删了）：
  算不出内容 UID，就用 `"x" + sha1(normcase(path))[:15]` 造一个**合成 UID**，照样同步——目的只是"
  别把笔记弄丢"。合成 UID 带 `"stale": true`；哪天同名文件重新出现、内容 UID 算出来了，
  把合成记录**并进**内容 UID（并集 + 墓碑，不删），再清掉合成键。
- **一个 UID 对多个本机路径键**（上面那本书就是：Desktop 一份、Downloads 一份）：
  合并结果**写回所有映射到该 UID 的本机键**——你从哪个路径打开都看得见同一份笔记。
- **本机没有这本书**：远端并回来的进度/划线先存进 UID 视图（`sync_state.json` 的 `remote_only`），
  屏幕上**不出现**打不开的书架行（现有"移出书架"那套不动）；等文件出现、UID 算出来时自动接上。
  设置页能看到一行"远端另有 3 本本机没有的书"。

### 5.2 四个同步对象（都是 JSON，UTF-8，不带 BOM）

**progress**（进度，按 UID）
```json
{"v": 1, "schema": 1, "updated_at": 1762950000.123, "device": "home-pc",
 "books": {"3f9a1c2b7d4e5f60": {"title": "雨", "pos": 183631, "total": 366182,
                                "at": 1762949990.5, "dev": "office-nb"}}}
```
> `schema` 是**正文抽取管线的版本号**（epub→纯文本那套规则改过就 +1）。`pos` / `start` / `end` 都是
> 抽取之后的字符偏移，不是文件字节偏移——两台机器 app 版本不同、抽取规则改过时，同一个 UID 的偏移含义会变。
> 规则：`schema` 不同、或 `total` 相差超过 0.5% → **这个 UID 只同步"读过/标题/书签文本"，
> 不写 `pos`/`start`/`end`**，日志提示一次"这本书的锚点版本不一致，进度这轮没同步"。宁可不跳，也不跳到错页。

**annotations**（划线/书签，按 UID；`id` 里的毫秒数就是该项的时钟）
```json
{"v": 1, "schema": 1, "updated_at": 1762950000.1, "device": "home-pc",
 "books": {"3f9a1c2b7d4e5f60": {
   "highlights": [{"id": "h1762949000123", "start": 1024, "end": 1088, "text": "…",
                   "note": "", "color": "#f5c26b", "chapter": "第三章", "at": 1762949000.123}],
   "bookmarks":  [{"id": "b1762949100456", "pos": 4096, "snippet": "…",
                   "chapter": "第三章", "created": "2026-11-12 20:41", "at": 1762949100.456}],
   "tombstones": [{"id": "h1762948000000", "at": 1762949500.0}]}}}
```
> **写回时字段逐个并**：远端没带的本地字段（`color` / `chapter` / `created` / `note`）保留本机的值，
> 不让"照远端重建"把划线全变成默认色、书签丢掉章节名。远端缺 `text` 时用本地正文按 `start/end` 重新切一段补上。
>
> 兼容老数据：老条目没有 `at`，从 `id` 里抠毫秒（`b1762949100456` → `1762949100.456`），抠不出来当 `0`。

**stats**（账本：`sessions` 是唯一真相，`days` / `books` 跟着一起传，只用于"别把历史越并越少"）
```json
{"v": 1, "updated_at": 1762950000.2, "device": "home-pc",
 "sessions": [{"k": "3f9a1c2b7d4e5f60", "title": "雨", "start": 1762945200.0,
               "sec": 905.0, "chars": 5127.0, "end": 1762947405.0, "gaps": [[600, 120]]}],
 "days": {"2026-11-12": {"n": 3, "sec": 2715.0, "chars": 15381.0}},
 "books": {"3f9a1c2b7d4e5f60": {"n": 3, "sec": 2715.0, "chars": 15381.0,
                                "first": "2026-11-11 10:59", "last": "2026-11-12 20:41",
                                "title": "雨"}}}
```
> `k` 改成 UID 后，两台机器的同一本书才会并成"一本书"。老账本里的路径键在合并时按 5.1 的映射换掉，
> 换不出来的（书已经不在了）保留原键——那些是不会再涨的历史，并集里当"另一本书"存着，比丢掉强。
>
> **为什么要传 `days` / `books`**：`normalize()` 只留最近 400 场会话（`KEEP_SESSIONS`），但日账能留 420 天。
> 只靠"从 sessions 重算"的话，历史超过 400 场的那天，一同步就会把更早的日账、书账、
> "这本书哪年开始读的"永久删掉——这正是反目标里不许发生的事。合并规则见 §7。

**settings**（只同步"跨机器有意义"的键）
- 同步：`font_size font_family line_spacing paragraph_spacing page_margin_chars first_line_indent
  paragraph_indent page_overlap_lines arrow_scroll_lines text_color bg_color opacity lowkey_key
  peek_key boss_key solid_key safe_key anki_deck anki_swallow_keys anki_conceal feed_lowkey
  feed_type_scheme auto_bg_solid ghost_mode antishot_mode restart_on_unlock`
- 不同步（设备本地）：`window_width window_height last_local_file last_local_pos safe_doc_path
  local_book_positions local_book_totals local_book_times local_book_hidden local_book_uid`
- `local_book_hidden`（移出书架）单独按 UID 并集进 progress，不进 settings。

### 5.3 本地 `sync_state.json`（新增，不进 git；**同步自己的运行时状态只能放这里**）

```json
{"device_id": "9f2c1a...", "device_name": "home-pc",
 "kinds": {"progress": {"rev": "d41d8cd9", "sha256": "…", "pushed_at": 1762950000.5, "dirty": false},
           "annotations": {...}, "stats": {...}, "settings": {...}},
 "uids": {"<本机路径>": {"uid": "3f9a1c2b7d4e5f60", "size": 366182, "mtime": 1762...}},
 "settings_clock": {"font_size": 1762950000.4, "text_color": 1762949000.1},
 "remote_only": {"<uid>": {"title": "雨", "pos": 0, "highlights": 0}},
 "pending": [], "last_error": "", "last_ok_at": 1762950000.5}
```

为什么单开一份：`config.json` 的写入权在 app 手里，`save_config()` 每翻一页就用内存副本整份重写一次
（`main.py:5238 → 5634`，非原子 `open(...,'w')+json.dump`）。同步线程往 config.json 里塞
`last_ok_at` / UID 缓存 / 时钟，下一帧翻页就没了。所以：

| 放哪 | 放什么 | 写入者 |
|---|---|---|
| `config.json` 的 `sync` 段 | 只有**用户能改的**：开关、地址、token、设备名、同步范围、间隔 | 设置页保存（走 `w.save_config()`） |
| `sync_state.json` | 一切**运行时**状态：rev、UID 缓存、设置时钟、待推队列、上次结果 | 同步线程自己（自己加锁、原子写） |

`sync_state.json` 丢了最多是"多传一次、重新算一遍 UID、设置时钟归零（那一次设置同步会按内容兜底，
不会丢字）"，不丢任何阅读数据。

## 6. 同步协议（7 个端点，够用且好测）

所有请求：`Authorization: Bearer <token>`；`Content-Type: application/json`；只走 HTTPS
（唯一的例外是 `http://127.0.0.1` / `localhost`，给本机 `wrangler dev` 联调用）。
响应统一 `{"ok": true, ...}` 或 `{"ok": false, "err": "..."}`。

| 方法 | 路径 | 作用 | 失败语义 |
|---|---|---|---|
| GET | `/v1/ping` | 自检：`{ok, server_time, version, bucket}` | 超时/401 直接告诉人 |
| GET | `/v1/manifest` | 四个对象的 `{kind: {rev, size, updated_at}}`（Worker 里 `list+head` 现算，不额外维护清单文件） | — |
| GET | `/v1/state/{kind}` | 拿对象本体，响应头 `ETag: <rev>` | 404 = 远端还没有，首次上传 |
| PUT | `/v1/state/{kind}` | `If-Match: <rev>`（新建时 `If-Match: *`）；成功 200 `{rev}` | **412 = 别人先写了**，客户端重新拉→合并→重试（≤3 次） |
| POST | `/v1/blob/check` | `{sha256:[...]}` → `{missing:[...]}`，别重复传 | — |
| PUT | `/v1/blob/{sha256}` | 原始字节流，Worker 直接转给 R2（不缓冲）。**必须带 `Content-Length`**，否则 R2 的 `put` 会抛 `Provided readable stream must have a known length`；缺这个头就回 411 | 409 = 已存在（幂等，算成功） |
| GET | `/v1/blob/{sha256}` | 流式回传（不加 S3 预签名，省一套密钥） | 404 = 远端没有 |

**并发控制用 ETag 条件写**：R2 的 `put(key, body, {onlyIf:{etagMatches}})` 不满足时**返回 `null`**
（官方文档的原话：precondition 不满足时 `put()` returns null，object 不会写入），天然是原子的
（单用户、单对象，不需要 Durable Object）。新建对象用 `{onlyIf:{etagDoesNotMatch:"*"}}`。
退化方案（万一某个档位不支持条件写）：Worker 里 `head` → 比 ETag → `put`；R2 的写是**强一致**的
（官方：once the Promise resolves, all subsequent reads see it globally），单人场景下竞态窗口只有毫秒级，
可以接受。**两个方案都写在 Worker 里，用一个开关切。**

## 7. 合并规则（这里是全部风险所在，逐条写死）

原则：**合并不覆盖；两边都有就都留；只增不减（除了带墓碑的删除）。**

| 数据 | 规则 |
|---|---|
| 进度 | 按 UID 取 `at` 更晚的那个；`at` 相同取 `pos` 更大的；两边都有 `total` 取更大的（防止一边没读完字数） |
| 划线 | 按 `id` 并集；同 `id` 取 `at` 更晚（笔记可能两端都编辑过）；`end/start` 变了也算更新 |
| 书签 | 同上（按 `id`）；同一 `pos` 不同 `id` 的保留两条（宁多勿丢），列表里会并排显示 |
| 删除 | `tombstones` 记 `id` + 删除时刻；合并后凡是墓碑里 `at` 晚于该条自身 `at` 的就真的删掉；**墓碑永远保留**（不清理，几 KB 而已，省得老设备把它复活） |
| 账本 | **见 §7.2**（同一场不求和、只取 end 更晚的；`days`/`books` 取 max 而不是纯重算） |
| 设置 | **见 §7.3**（逐键时钟，不是整份 LWW） |
| 书文件 | 内容寻址：同一个 sha256 = 同一本书，谁先传谁算；两边都有就都不传。**永不删除远端 blob** |

**写回本地时**：只在"合并结果 ≠ 本地"时写；写之前把旧文件复制成 `xxx.json.bak`（滚动 2 份），
用现有的 `tmp + os.replace` 原子替换。任何一步失败 → 本地保持原样，日志记一行，下轮再来。

### 7.1 写回不能绕过后台的内存对象（这一条最容易做错）

`config.json` / `annotations.json` / `reading_stats.json` 都是**进程内对象整份重写**的：
`self.config`、`self.annotations`、`self.stats_data` 在内存里各有一份。如果同步线程直接改磁盘文件，
用户下一次翻页触发 `save_config()`，就会用内存里的旧值把刚并进来的东西盖掉——数据看着"同步了又回去"。

所以：**合并结果一律走主线程，写进内存对象，再调应用自己的保存函数。**

| kind | 主线程写回动作 |
|---|---|
| progress | `w.config` 里那四个 `local_book_*` 表按 UID 视图并进去 → `w.save_config()` |
| annotations | 并进 `w.annotations` → `w.persist_annotations()`（顺带 `apply_highlight_selections()`） |
| stats | 并进 `w.stats_data` → `reading_stats.save(path, data)`（现有函数，参数顺序 path 在前） |
| settings | 并进 `w.config` 的白名单键 → `w.save_config()` → `w.apply_style()` |

同步线程与主线程之间只传**纯数据**（dict），不传 Qt 对象；用 `pyqtSignal(dict)` 送回来，
主线程在槽里做上面四件事（每件都是毫秒级，不会卡界面）。

写回顺序也是固定的：**先写 `.bak` → 再改内存 → 再存盘**。存盘失败就把内存改回去（同一个函数里做），
不留下"内存新、磁盘旧"的半截状态。

### 7.2 账本：不能让"在读"越并越涨

**会话身份 = `(k, round(start))`；同一身份只留一条，取 `end` 更晚的那条**（不求和）。

为什么不能按"整条记录去重"：`add_span()` 会把新读的一段**并回同一场**、原地改 `sec/chars/end`，
所以"同 start、不同 sec"的两条是**同一场的新旧两个快照**，当两条独立记录并起来再求和就翻倍。

推演（原来的规则会踩的坑）：A 读到 10:30 上传（`sec=1800, end=10:30`）→ B 拉到后 10:36 接着读，
本地那份被 `add_span` 改成 `sec=2160, end=11:00` 再传 → A 再拉回。按 `(start,k,sec)` 去重会留下 1800 和 2160 两条，
`consolidate()` 再把同书相邻两场求和 → **3960**（一小时窗口记成 66 分钟），下一轮继续涨，而且"只增不减"永远修不回来。
按 `(k,start)` 取 end 更晚 = 2160 ✓，一轮就收敛。

其余规则：
- 不同身份的会话（两块真正不重叠的阅读时间）照旧**并集**，再 `consolidate()`（同书 15 分钟内并成一场、求和）——
  这是对的，两段墙钟本来就不重叠。
- 合并后每个会话 `sec = min(sec, end - start)`：墙钟跨度是硬上限。正常记录本来就满足（空闲超过 3 分钟会记成 gap），
  只有"两台同时在读、时间重叠"这种异常才被截断，截断时日志写一行（`截断: 雨 10:00 场 3960→3600`）。
- `days` / `books` **不是"只重算"**：字段级取 `max(重算值, 本机存的, 远端存的)`；`books[k].first` 取更早、
  `last` 取更晚。这样历史在 400 场以内时等于"重算"（数字天然正确）；历史超了 400 场时，
  存着旧日账的那边赢，**不会变少**。
- 幂等自检（要写成单测）：同一份账本并两次 == 并一次；A 并 B == B 并 A。

### 7.3 设置：逐键时钟，不是整份 LWW

- 白名单每个键一个"最后修改时刻"，存在 `sync_state.json` 的 `settings_clock`。
- 时钟从哪来：`save_config()` 每翻一页都会跑，但同步只比较**白名单子集**的取值——和上一份快照不同的键
  才把时钟设成 now。翻页不改字号 → 时钟不动 → 不会把另一台刚改的字号判成旧值。
- 合并：逐键比时钟取新的；远端没带时钟（老版本或第一次同步）时，只有本地缺这个键才用远端的。
- 时钟是各自本机的 `time.time()`，两台差几分钟只影响"几分钟内两台改同一个键"的极少数情况。

## 8. 客户端怎么落在这个仓库里

新文件 `sync_engine.py`（约 700–900 行，标准库，跟 `reading_stats.py` 一个风格：纯函数 + 一个线程类）。
`main.py` 加**接线**（约 200 行）：四个写回 handler（§7.1）+ 打脏点 + 设置页 + 托盘 + 定时器。
现有存盘逻辑（`save_config` / `persist_annotations` / `_stats_commit`）不改——同步只调用它们，不替换它们。

| 位置 | 加什么 |
|---|---|
| `SYNC_CONFIG` 默认段 | 见 §10 |
| `StealthReader.__init__` | 建 `SyncWorker`（不启动网络，只建对象）；`sync_signal = pyqtSignal(dict)` 接回主线程 |
| `apply_style` / 设置页保存 / `persist_annotations` / `_stats_flush` | 各自末尾 `self.sync_dirty(kind)`（只打标记 + 起 3 秒防抖定时器，不直接发网络） |
| 定时 | 启动 5 秒后拉一次；每 10 分钟心跳；退出前 `flush(3 秒超时)` |
| 托盘菜单 | 「☁ 立即同步」 + 状态行（"上次同步 2 分钟前" / "离线，等网络"） |
| 设置页 | 新增「同步」一节（见 §11） |
| 日志 | 每次成功/失败一行，`log_event` 统一走现有格式 |

**线程模型**：一个后台 `threading.Thread`（daemon），`queue.Queue` 收任务，**绝不碰 Qt 对象**；
结果通过 pyqtSignal 回主线程再动界面。所有网络调用 `connect timeout=5s, read timeout=15s`，
单次同步总预算 30 秒。

**触发时机（够用就好，不做实时）**
1. 启动后 5 秒（先把远端拉下来再让你看见书架）；
2. 本地写入后 3 秒防抖（连改十次也只发一次）；
3. 每 10 分钟一次心跳拉取（别的机器改过就能看见）；
4. 退出前（最多等 3 秒，等不到就留给下次）；
5. 手动「立即同步」。

**离线队列**：`sync_state.json` 里 `pending` 记"哪些 kind 脏了"，不含数据本身（数据永远从本地文件现读），
所以队列丢了也不会丢数据，最多多发一次。

## 9. Cloudflare 侧

```
cloudflare-sync/
├─ wrangler.toml        # name / main / r2_buckets / vars
├─ src/worker.js        # ~220 行：路由 + 鉴权 + 条件写 + blob 流式转发
└─ README.md            # 部署 7 步（wrangler login → bucket create → secret put → deploy → 绑域名）
```

```toml
name = "stealthreader-sync"
main = "src/worker.js"
compatibility_date = "2026-11-01"

[[r2_buckets]]
binding = "BUCKET"
bucket_name = "stealthreader-sync"

[vars]
TOKEN_TTL_DAYS = "365"
```

Worker 里只做四件事：
1. `Authorization` 常量时间比较（`crypto.subtle.timingSafeEqual` 不可用时用手写异或比较）；
2. 四个 kind 白名单（`progress|annotations|stats|settings`），其它一律 404，防止被当免费网盘；
3. 条件写（`onlyIf.etagMatches` / `*`），成功顺手把旧版本存进 `v1/state/history/{kind}/{ts}.json`，只留 5 个；
4. blob 只允许 `sha256` 形式的 key、单次 ≤ 50 MB（epub 最大 8 MB，留余量）。

**成本**：4 个 JSON 对象、每天几十次请求、最多 19 MB 存储 —— 免费额度（Worker 10 万次/天、R2 10 GB 存储、
Class A 100 万次/月）连零头都用不到，正常是 0 元。

## 10. 配置项（`config.json` 新增一段，默认关闭）

```json
"sync": {
  "enabled": false,
  "base_url": "",                 // 例如 https://sync.example.com（不要用 *.workers.dev，见 §14）
  "token": "",                    // 只存本机；界面里是密码框
  "device_name": "home-pc",
  "kinds": {"progress": true, "annotations": true, "stats": true, "settings": true},
  "books": false,                 // 书文件（19 MB，默认不同步）
  "debounce_sec": 3,
  "interval_sec": 600
}
```
这一段里**只有用户能改的东西**（所以只由设置页写）。`device_id`、上次同步结果、UID 缓存、
设置时钟一律在 `sync_state.json`（§5.3）——`config.json` 每翻一页被整份重写一次，塞这里会被擦掉。

## 11. 界面（三处，都很小）

1. **设置 → 同步**：开关 / 服务器地址 / Token / 设备名 / 同步哪些内容（四个勾 + 书文件）/ 「连通性自检」/
   「立即同步」/ 一行结果（`上次成功 20:41，推了 2 个对象，187 ms`）。
2. **托盘菜单**：「☁ 立即同步（上次 20:41）」。
3. **首次启用时的对话框**（只有这一次）：
   - 远端空 → "上传本机数据（10 本书的进度、6 本的笔记、8 场账本）"；
   - 远端有、本机也有 → **默认合并**，并显示会新增什么（"+3 本书的进度、+2 条划线"），另一个选项是"先备份本地再合并"；
   - 永远不提供"用远端覆盖本地"这个按钮——这条反目标不做。

## 12. 失败与边界（照着这张表写代码就不会翻车）

| 情形 | 表现 |
|---|---|
| 没网 / DNS 挂了 | 不弹窗；托盘显示"离线"；队列留着；退避 5s→1m→5m→15m 封顶 |
| token 错（401） | 停止重试，设置页红字"Token 不对"；不动任何数据 |
| 服务器 500（Worker 挂了） | 同"没网"，退避重试 |
| 412 冲突（另一台先写了） | 拉 → 合并 → 重试，最多 3 次；3 次还冲突 → 记日志，5 分钟后再说（数据没丢，只是这一轮没并上） |
| 远端对象坏了（JSON 解析失败） | 不覆盖远端，也不动本地；日志报"远端 progress 解析失败"，跳过这一 kind |
| 本地文件同时被写 | 合并结果**只从主线程写**（§7.1，写进内存对象再调应用自己的保存函数），所以不存在"两方同时写同一个文件"；同步线程只读，读之前 `os.stat` 两次确认 mtime+size 稳定 |
| 时钟偏差（两台差几分钟） | 字段级 LWW 用各自本地时间，误差可接受；同一个字段两边都改且相差 < 1 分钟时，可能选错一边——概率极低，接受 |
| 两个进程同时跑同步 | `sync_state.json.lock`（`os.open(..., O_CREAT|O_EXCL)`）+ 30 秒过期，拿不到锁就跳过这轮 |
| 书文件传到一半断了 | 内容寻址天然可重传（同一 sha256 重传，R2 覆盖无害）；`blob/check` 先问缺哪些 |
| 磁盘满了 / 写不进去 | 写回失败 → 内存改回去、本地保持旧文件（原子替换保证），日志报错，同步暂停到下次启动 |
| 锚点版本不一致（`schema` 不同 / `total` 差 > 0.5%） | 这个 UID 只同步"读过/标题/笔记文本"，不写 `pos`/`start`/`end`；日志提一次，不弹窗 |
| 书文件已经不在（算不出内容 UID） | 用合成 UID（`"x"+sha1(path)[:15]`，带 `stale`）照样同步，笔记不丢；文件回来后并进内容 UID |
| 一个 UID 对多个本机路径 | 写回**所有**映射到它的本机键，从哪个路径打开都看得见 |
| 本机没有这本书 | 存进 `remote_only`，书架上不出现打不开的行；设置页显示"远端另有 N 本本机没有" |

## 13. 安全与隐私

- 传输只有 HTTPS；Worker 只认 Bearer token（`wrangler secret put SYNC_TOKEN`，不进仓库）。
- 服务端能读到明文（你的书页摘录、笔记）。**如果这不能接受**，二期加端到端加密：
  口令 → PBKDF2 → AES-256-GCM 加密后再上传，服务端只见密文；因为合并本来就在客户端做，加了加密不影响合并。
- R2 bucket 不公开（`r2.dev` 关闭），只能过 Worker。
- token 泄露的最坏结果：别人能读写你这一桶书摘 —— 所以 token 用 32 字节随机串，换 token＝在 Worker 里
  `secret put` 覆盖，客户端改一处即可。

## 14. 中国大陆可达性（先说清楚，别到时候白忙）

**在这台机器上量过（2026-11-12 20:5x，本机 → Cloudflare）**

| 目标 | 结果 |
|---|---|
| `https://www.cloudflare.com/cdn-cgi/trace` | **HTTP 200，430 ms，`colo=SIN`**（新加坡边缘） |
| `https://www.cloudflare.com/` | HTTP 200，537 ms |
| `https://1.1.1.1/cdn-cgi/trace` | **超时**（8 秒放弃）——1.1.1.1 这家在这里是死的 |
| DNS `www.cloudflare.com` / `workers.dev` | 11 ms / 19 ms 都能解析（解析没被污染） |

结论：**CF 的 HTTP 边缘在这里是通的，但一次往返 ~430 ms**，而且 `*.workers.dev` 这种共用子域
在国内经常被单独黑洞（DNS 能解析、连上去被重置），**别用它**。挂**自定义域名**（CF 代理）。
即便挂了自定义域名也**只是"通常能通"**，不是保证：延迟常在 150–500 ms，偶尔被干扰。

设计上对应的三件事：
1. 同步全在后台 + 超时短（连 5 s / 读 15 s）+ 退避，不通就是"离线"，不影响读书；
2. 一次同步 = 1 次 manifest + 最多 4 次 GET/PUT，**四个 kind 并发拉**（4 个短连接），
   按 430 ms 往返算总耗时 ~1 s 而不是 2 s+；
3. 客户端只认 `base_url`，真不行换掉（自己的服务器、坚果云 WebDAV 网关都行）；
   设置页放「连通性自检」，直接量出 DNS / TLS / ping / 一次 PUT 的耗时，别靠感觉。

书文件（19 MB）在 430 ms 的链路上首次上传估计要几分钟（还受上行带宽限制），
所以默认**不同步书文件**；打开时显示预计流量。

## 15. 分三期，每期都能单独验收

**一期（最小可用，约 2–3 天）**
- Worker + R2 + 四个 kind 的拉/推/合并 + 主线程写回 + 设置页 + 托盘 + 首次同步对话框
- 验收：两台机器（或同一台换 `APP_DIR` 模拟）→ 进度/笔记/账本互相同步；拔网线 → 不报错、恢复后自动并；
  **两台轮流读同一本书三轮，账本秒数只涨真实读的那部分**（§7.2 的推演）。

**二期（备份与恢复）**
- 书文件同步（内容寻址、先 check 再传）、历史版本页签（5 个旧版本一键回滚）、端到端加密开关
- 验收：删掉本地 `annotations.json` → 一键从远端恢复；改坏远端某对象 → 回滚到上一版。

**三期（省心）**
- 冲突可视化（哪本书、哪个字段冲突，并排给你看）、同步日志页、按书选择性同步
- 验收：故意制造冲突（两台同时改同一本书的进度）→ 界面能说清"并了什么"。

## 16. 测试计划（不部署 CF 也能验 90%）

1. **纯函数（`probe_sources` 里加约 32 条）**：四个 merge 函数 + UID 迁移 + 合成 UID + 一 UID 多路径 +
   墓碑 + 时间戳解析（老 `id` 抠毫秒、老 `created` 字符串）+ **账本收敛**（同 start 不同 sec 只留 end 更晚的、
   并两次 == 并一次、A 并 B == B 并 A、`sec ≤ end-start` 的截断）+ `days`/`books` 的 max 规则
   （超 400 场时旧日账不缩水）+ 设置逐键时钟 + `schema` 不一致时拒写偏移。
2. **假后端端到端（`tests/fake_sync_server.py`，`http.server` 一个内存实现，含 ETag/412/401/超时/断线开关）**：
   首传、拉取、真冲突、离线重试、token 错、书文件去重、远端坏 JSON、
   **两台模拟轮流读同一本书 → 三轮同步后"在读"总数不涨**（这条直接用 7.2 的推演做夹具）。
3. **真机自检 CLI**：`python sync_engine.py --selftest <base_url> <token>` → ping / PUT / GET / 412 / blob 五步，
   打印每步耗时，部署完先跑这个。
4. **实机**：两台机器（或两个 `APP_DIR`）轮流读同一本书，看进度是不是"后读的赢"、笔记是不是"两边都在"、
   账本秒数是不是**只涨该涨的那部分**。

## 17. 部署清单（你只需要做 3 件事，其余我写脚本）

本机已经有的前提（2026-11-12 查过）：`node v24.17.0` / `npm 10.9.2` / `pnpm 10.34.5` 都在，
`wrangler` 用 `npx wrangler` 跑，不用全局安装。

1. `npx wrangler login`（浏览器授权一次）；
2. `npx wrangler r2 bucket create stealthreader-sync`；
3. 给 Worker 绑一个**你自己的子域名**（比如 `sync.yourdomain.com`），并在设置页填 `https://sync.yourdomain.com` + token。

其余（`wrangler.toml`、`worker.js`、`secret put`、自检）都在 `cloudflare-sync/README.md` 里按步骤做。

## 18. 我替你做的决定（不同意就说，改起来都很便宜）

| # | 决定 | 为什么 |
|---|---|---|
| 1 | 默认**不同步书文件**，只同步数据 | 19 MB 在国内链路上首次要几分钟；数据和书可以分开决定 |
| 2 | 书身份用**内容 sha256 前 16 位**，不用路径；算不出来的用合成 UID | 路径换机器必废；内容寻址顺带让书文件去重；合成 UID 是为了"文件删了笔记不许丢" |
| 3 | 账本传 `sessions` + `days`/`books`，**会话身份 `(k,start)` 取 end 更晚的**，`days`/`books` 取 max | 只传 sessions 会在超 400 场时削掉旧日账；按整条记录并集会让"在读"每轮翻倍 |
| 4 | 冲突一律**客户端合并**，服务端只做条件写 | Worker 保持 220 行的"笨"，好审计、好测 |
| 5 | 删除只带墓碑，**远端永不删对象/书** | 你的底线：数据只能变多不能变少 |
| 6 | 同步对象是"一个 kind 一整份 JSON"，不是逐条 | 单人 + 5–8 KB，整份最省事；逐条同步是给协作用的 |
| 7 | 不做后台常驻，跟 app 生命周期 | 少一个守护进程，少一类 bug |
| 8 | 设计文档放在 `docs/cloudflare-sync-design.md` | 跟现有两份设计文档同一层，不另起 `specs/` 目录 |
| 9 | **运行时状态全部放 `sync_state.json`**，config.json 只留用户能改的 | `save_config()` 每翻一页整份重写，塞进去会被擦掉 |
| 10 | 合并结果**只从主线程写回**内存对象再存盘 | 后台线程直接改文件，下一帧翻页就被内存副本盖掉 |
| 11 | 设置用**逐键时钟**，不用整份 LWW | config 没有 updated_at，整份 LWW 等于"翻页=改设置" |
| 12 | 同步对象带 `schema`（抽取管线版本），不一致就不写偏移 | `pos`/`start` 是抽取后的字符偏移，跨版本含义会变 |

## 19. 留给你的四件事（有默认值，不回也能开工）

1. **Cloudflare**：有账号吗？有一个能挂子域名的域吗？
   没有域名也能开工：`npx wrangler dev` 在本机跑同一个 Worker（`http://127.0.0.1:8787`），
   客户端指过去就能把全流程跑通、并发/冲突/断线全验掉；正式用再挂域名、换 base_url 就行。
   （明文 HTTP 只对 `127.0.0.1` / `localhost` 放行，别的地址必须 https。）
2. **书文件同步**：要不要？（默认不要）
3. **几台机器、都 Windows 吗**？（影响路径规范化与冲突策略的细节，默认"两台 Windows"）
4. **明文存云端接受吗**？（默认接受；不能接受就把二期的 E2E 提到一期，晚 1 天）

## 20. 风险登记

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| 国内访问 CF 不稳 | 中 | 高 | 后台 + 退避 + 可换 endpoint + 自检 |
| 合并逻辑出错导致丢笔记 | 低 | 高 | 只并集 + 墓碑 + 写前 `.bak` + 纯函数 32 条单测 + 假后端端到端 |
| 同步与 app 的持久化打架（内存副本盖掉合并结果） | 中 | 高 | 写回只走主线程 + 内存对象 + 应用自己的保存函数（§7.1）；运行时状态不进 config（§5.3） |
| 账本越并越大 | 中 | 高 | 会话身份 `(k,start)` 取 end 更晚 + `sec ≤ end-start` 截断 + 三轮收敛单测（§7.2） |
| 首次同步把本地覆盖 | 低 | 极高 | 不提供"覆盖本地"选项；首次只合并 + 显示将新增什么 |
| 书 UID 计算拖慢启动 | 低 | 低 | 后台线程 + size/mtime 缓存 |
| 你换机器后书路径变了 | 中 | 低 | 进度按 UID 走；书架靠重新添加文件再按内容认回来 |
| 抽取管线改版导致偏移错位 | 低 | 中 | 同步对象带 `schema` + `total` 校验，不一致就不写偏移（§5.2） |

## 21. 这份设计被复核过一遍，改了六处

写完之后让一个没参与起草的独立 agent 拿着 `main.py` / `reading_stats.py` 的真实代码复核，
它挑出六条硬伤，都改进了正文（留在这里是为了让你知道哪些地方原来想错了）：

| # | 原来的写法 | 问题 | 现在 |
|---|---|---|---|
| 1 | 同步线程把合并结果写进磁盘文件 | `save_config()` 每翻一页用内存副本整份重写，后台写进去的东西下一帧就没了；`last_ok_at` / UID 缓存放 config 里同样被擦 | §7.1 只从主线程写回内存对象再调应用自己的保存函数；§5.3 运行时状态全搬进 `sync_state.json` |
| 2 | 会话按"整条记录"去重并集 | `add_span()` 会把新读的一段并回同一场改 `sec`，同一场的新旧快照会被当成两场再求和 → **每轮同步都在涨** | §7.2 身份 `(k,start)`、取 `end` 更晚的、`sec` 截到墙钟跨度 |
| 3 | `days`/`books` 从 sessions 重算 | `normalize()` 只留 400 场，重算会把更早的日账/书账/"哪年开始读的"删掉 | §7.2 字段级 `max(重算, 本机存, 远端存)` |
| 4 | 假设每本书都有文件 | 现场就有 6 条划线挂在已删除的 `Desktop\当下的力量….epub` 上；同一本书还对应两个本机路径 | §5.1 合成 UID（带 `stale`）、一 UID 多路径写回、本机没有的书进 `remote_only` |
| 5 | 设置走文件级 LWW | config 没有 `updated_at` 且每翻一页重写，等于"翻页=改设置"，会把另一台改的字号判成旧值 | §7.3 逐键时钟，存 `sync_state.json`，只比较白名单子集的取值变化 |
| 6 | UID 锁住文件，就以为偏移也对得上 | `pos`/`start` 是**抽取后**的字符偏移，管线改版后同一本书含义会变 | §5.2 带 `schema` + `total` 校验，不一致就不写偏移 |

## 22. 落地时又改的七处（都是真跑出来的，不是想出来的）

一期写完之后，用真数据对着生产跑了「首传 -> 拉取 -> 再并一轮」，暴露了七个设计里没想到的问题，
全部已改并写进代码注释。列在这里是因为其中三条是**会丢数据**的，值得单独记一笔。

| # | 症状 | 原因 | 改法 |
|---|---|---|---|
| 1 | 真账本「09:59 那场 65 秒」被削成 60 秒 | 截断按 `sec <= end-start`，但 `sec` 是"数了几拍"，`end` 是最后一次落点，正常记录就比跨度多一拍 | `SEC_SLACK = 5`，截到 `span + 一拍`；这一条**差点吃掉了真数据** |
| 2 | 9 条划线只传上去 3 条 | `annotations_view` 只用"书架上有进度记录"的书建 UID → 只翻两页就关掉的书、文件被挪走的书，笔记永远上不去 | `annotations_view` / `_collect_tombstones` 用 `uid_for()` 兜底（文件没了也有合成 UID） |
| 3 | 两台机器第一次同步，字号永远传不过去 | 设置时钟都是 0（从没改过），"时钟相同留本地" → 谁都不接受对方的 | 每条设置第一次见到给哨兵 `1.0`（= "很久以前"），**时钟相同让远端赢**；真改过的键时钟是"现在"，不会被盖 |
| 4 | 删掉的划线下一轮又回来了 | 墓碑只从远端合并结果里取，本地新删的没人记 | 每轮拿本地 ID 集合跟"上一轮合并结果"比，少掉的记墓碑（整个 UID 消失不算删除） |
| 5 | 主线程写回时"设置没生效" | `merge_settings` 返回 `(values, clock, notes)` 三元组，调用处按二元组解包 | 统一返回"同步对象形状 + 说明"二元组，四个 kind 一致 |
| 6 | 首传慢：14 秒里 10 秒在建连 | `urllib` 每次请求一条新连接 | 换 `http.client` + 复用一条 keep-alive 连接（同一个自检从 14.5 秒降到 7 秒左右） |
| 7 | `--selftest` 把"一本假书"永久写进了真进度 | 自检直接 `PUT /v1/state/progress` 一条 `selftest` 记录 | 自检先读原对象、写完立刻**原样放回**（§10 端点语义不变） |

### 22.1 书文件同步：实际是怎么做的

当初只写到"内容寻址、先 check 再传"。真跑起来发现光上传不够——**另一台机器没法还原书名**，
只能拿到一串 sha256。所以补了两件很小的事：

1. 上传时带 `X-File-Name`（`encodeURIComponent`，中文不能直接进 HTTP 头），Worker 存进 R2 的
   `customMetadata.name`，并做一次"只留文件名、去路径分隔符"的清洗；
2. 新增 `GET /v1/blobs` → `{blobs:[{sha,size,name}]}`。**注意 R2 的 `list()` 默认不返回
   customMetadata，必须显式 `include: ["customMetadata"]`**（这一条查了半天）。
3. 同步界面里加「从云端补齐书」：列出云端所有书，把本机没有的按原名下到书架目录。
   **只在这个按钮里写磁盘**——同步轮里绝不偷偷下文件（19 MB 无声出现太吓人）。
4. 老对象没有原名时，客户端带 `X-Fix-Name: 1` 重传一次同样的字节，Worker 用
   `onlyIf.etagMatches` 就地补元数据（内容不变）。

实测（本机 9 本 19 MB，链路约 500 KB/s 上行 / 160 KB/s 下行）：首传 38.6 秒，
拉一本 0.3 MB 约 3 秒，整批拉回 22 MB 约 135 秒。所以单请求超时从 20 秒放宽到 45 秒，
书文件开关仍然默认关闭（§18 第 1 条），打开时提示首次要几分钟。

---

**下一步**：这份设计过了，就按它出实施计划（`superpowers:writing-plans`），然后一期动手。

## 23. 增量：这一轮到底发了几个请求

第一版每轮都是"全量"：问 manifest、四个 kind 全下、合并完全写回、再问一遍 `/v1/blobs` 并重算 9 本书
（19 MB）的 sha。**一轮 10 个请求，什么都没改也照发**。R2 的写是要计数的，而且每写一次就换 rev，
另一台机器看见 rev 变了又要白跑一轮——两台机器都在跑，就等于每 60 秒互相踢一次。

现在按 kind 做增量，判断只有两条：

| 判断 | 依据 | 省掉 |
| --- | --- | --- |
| 远端动没动 | manifest 里的 `rev` 跟 `sync_state.json` 里记的一样 | 这一次 GET（用缓存里那份内容） |
| 要不要写 | 合并结果的内容指纹 == **远端那份**的指纹 | 这一次 PUT（不换 rev） |

内容指纹 `_view_hash()` 只算内容：`updated_at` / `device` / `rev` 不参与。**比的是远端，不是上一轮的缓存**——
另一台机器刚改过、我们只是拉下来时，合并结果就等于远端，这时写回去纯属白写一次。

### 23.1 量到的数（生产，2026-11-13）

| 场景 | 以前 | 现在 |
| --- | --- | --- |
| 空闲一轮（什么都没改） | 10 个请求（1 manifest + 4 GET + 4 PUT + 1 blobs） | **1 个**（只有 manifest） |
| 读了一页（进度 + 账本变） | 10 个 | **3 个**（manifest + 1 GET + 1 PUT） |
| 别的机器改了划线 | 10 个 | **3 个**（manifest + 1 GET + 0 PUT：合并结果等于远端，不写） |
| 重启后第一轮 | 10 个 | 5～7 个（缓存是空的，得先认一遍；之后回到 1 个） |

`sync_state.json` 因此从 ~10 KB 涨到 ~27 KB（四个 kind 各存一份上次内容 + 指纹），可以忽略。

### 23.2 书文件：两级缓存

- `book_hash`：路径 →（大小, mtime, sha）。19 MB 的书不重算 sha，改过才重算。
- `book_uploaded`：sha → 远端记着的原名。有它就**不问** `/v1/blobs`；改过名才发现名字对不上，只重传那一本。

于是「书没变」的一轮对书文件是 **0 个请求**（以前每轮 1 个 list + 9 次全量 sha 计算）。

### 23.3 顺手修的两件

- **单类失败不再拖累其他三类**：一个 kind 抛错只记一条 note，其他三个照常。以前一个 kind 失败整轮就废了。
- **一轮的请求数进日志**：「同步完成：annotations, progress, settings, stats（1 个请求：GET 1 / PUT 0）」。
  `STEALTHREADER_SYNC_DEBUG=1` 时再逐 kind 打印「跳过 / 要写，差在哪些字段」，排查"为什么它老在写"用这个。

### 23.4 想过、但没做的

| 想法 | 为什么不做 |
| --- | --- |
| `If-None-Match` 304 | rev 没变根本不发 GET，比 304 更省；rev 变了就必须拿内容，304 用不上 |
| 把四个 GET/PUT 合成一个批量接口 | 省的是 4 次往返（约 3～6 秒），可同步是后台跑的，没人等这几秒；批量 PUT 还要在 body 里塞四个 `If-Match`，复杂度和收益不划算 |
| gzip 压请求体 | 账本现在 ~8 KB，压完省几 KB，但省不了往返；等真到几百 KB 再说 |
| 划线按书分对象（`v1/state/annotations/<uid>.json`） | 对象数变多、每轮请求数变多；现在 9 条划线一个对象 4 KB，完全够 |
| 书文件断点续传 | 8 MB 一本书、45 秒超时；失败了下次整本重传，但一本书就一次，忍了 |


## 24. 多端阅读页：手机/平板/电脑直接看同一本书

桌面端只能在 Windows 上读。这一节是把"同一份进度、同一批排版设置"搬进浏览器：
电脑浏览器、手机浏览器打开 `https://sync.noteklip.org/r/`，输一次 token，就能接着读。

### 24.1 为什么用 foliate-js，而不是自己写解析

| 方案 | 结论 |
| --- | --- |
| foliate-js（选它） | MIT；原生 ES module，没有构建步骤；EPUB/MOBI/KF8/FB2/CBZ 都认；`renderer.setStyles()` 能直接把 CSS 注进正文 iframe，排版设置不用自己算 |
| epub.js | 只认 EPUB；要 preact/polyfill 打包；分页器对 CJK 的标点挤压不如 foliate |
| 自己用 zip.js 解 + 自己排版 | 光"分页"这一件事就要处理字体度量、标点悬挂、双栏、滚动模式，几千行起步，不值 |

锁定上游 commit `78914aef4466eb960965702401634c2cb348e9b1`（2026-05-01），整包 **257.7 KB / 16 个文件**，
放在 `cloudflare-sync/web/vendor/foliate-js/`，`VENDOR.md` 记了来源、许可证（含 `vendor/zip.js` 的 BSD-3
和 `vendor/fflate.js` 的 MIT）和升级办法。**不打包、不转译**：浏览器直接 `<script type="module">` 加载，
所以 Worker 只需要把静态文件原样发出去。

### 24.2 三种设备，两条通道

| 通道 | 谁用 | 实现 | 状态 |
| --- | --- | --- | --- |
| `/r/` 单页应用 | 电脑浏览器、手机浏览器、平板 | foliate-js + `app.js`（约 18 KB） | **已上线** |
| `/k/<uid>?p=N` 服务端渲染的极简 HTML | Kindle 实验浏览器（老 WebKit） | Worker 里把纯文本切成一页页 HTML | 二期，未做 |
| 桌面端 | Windows | 原有 PyQt5 | 不变 |

Kindle 那条路不共用前端：Kindle 的浏览器跑不动 ES module 和 iframe 分页，硬接只会得到白屏。
二期的做法是 Worker 直接用桌面端上传的章节表把纯文本切片，服务端出 HTML。

### 24.3 一次阅读的完整链路

```
打开 /r/ ──► GET /v1/ping（Cookie 鉴权，401 就显示登录框）
   │
   ├─ 登录：POST /v1/login {token} ──► Set-Cookie: srtoken=…; HttpOnly; Secure; SameSite=Lax
   │
   ├─ 书架：GET /v1/shelf ──► R2 书清单 × progress 里的 pos/total 拼成一行行
   │
   ├─ 点一本：GET /v1/lite/<uid>/index.json（几 KB 的章节表，可无）
   │           GET /v1/blob/<sha>（**只拉这一本**，zip.js 自己按 Range 分块取）
   │
   ├─ 翻页：foliate 的 relocate 事件 ──► pos = round(fraction × total)
   │       防抖 2.5 秒 ──► GET /v1/state/progress（拿 ETag）──► PUT（只改我这一行）
   │
   └─ 改排版：点一下 ──► 立刻改本地 CSS ──► PUT /v1/state/settings（只动改到的 key 的 clock）
```

关键取舍：浏览器**不做"读全量 → 合并 → 写回"**。桌面端那套合并逻辑（按 `at` 最新的一行赢、
`total` 取 max、划线的 tombstone 并集）在 JS 里重写一遍必然会和 Python 版跑偏。浏览器只做
"把我在读的这一行的 7 个字段替换掉，其他行原样带回去"，再靠 `If-Match` 保证不覆盖别人：
冲突就重来（最多 3 次），语义和桌面端合并出来的结果一致。

### 24.4 一个真踩到的坑：R2 整份取也会填 `range`

Worker 加 Range 支持时，第一版写成"`obj.range` 存在就回 206"。本地内存版 R2 不填这个字段，
测不出来；**真 R2 整份取的时候也会填 `{offset: 0, length: size}`**，于是线上所有书都变成
`206 Partial Content + Content-Range: bytes 0-N/N`。

后果不是"网页放不出书"，而是更隐蔽的：桌面端 `blob_get()` 只认 200，会直接报「HTTP 206 下书失败」。
也就是网页一上线，另一台机器的"从云端补书"就废了。

修法：判"是不是部分响应"看**请求**有没有带 Range，不看 `obj.range` 有没有值；同时把内存版假 R2
也改成整份取时填 `range`，这样这个 bug 在本地就能被 `worker_selftest.mjs` 抓住（现在 60 条断言里
有一条专门盯它）。

### 24.5 另一个真踩到的坑：`os.replace` 被占用

上线当天日志里出现「同步状态写不进去：[WinError 5] 拒绝访问：sync_state.json.tmp → sync_state.json」。
原因是 Windows 上那个瞬间有别的手（杀软 / 搜索索引 / 探针）开着目标文件。后果不只是这一轮没写进去：
`sync_state.json` 里存着每个 kind 的 rev 和书文件缓存，丢了下一轮就会把没变的 kind 全下载一遍。

现在 `write_json_atomic()` 会重试 6 次（0.05+0.10+…+0.25 秒），全失败就直接覆盖写——
宁可少一层原子性，也别把缓存丢了。探针里有 5 条断言盯着这条路径（包括"重试全失败也要写进去"）。

### 24.6 章节表：`v1/lite/<uid>/index.json`

桌面端本来就要为每本书抽全文（算字数、算进度），顺手把结果里现成的章节偏移存下来：

```json
{"schema": 1, "uid": "481f48cfaf0aa453", "total": 357534, "generated_at": 1763000000.0,
 "sections": [{"title": "第一章 …", "start": 619, "level": 1}, …]}
```

- **坐标就是桌面端那套**：`total` 是抽出来的纯文本字符数，`start` 是该章正文在纯文本里的字符偏移。
  所以网页上的 `pos` 和桌面端的 `pos` 是同一个数，不需要任何换算。
- 只在一本书"大小或 mtime 变了"时重算重传（`sync_state.json` 里的 `book_index` 记着）。
  9 本书首次共 **9 个 PUT**，之后 0 个。
- 大小几 KB 到几十 KB（146 章的那本约 8 KB），走 `PUT /v1/lite/<uid>/index.json`，随写随覆盖。

### 24.7 排版：桌面端的键 ↔ 浏览器的 CSS

| 设置键 | 桌面端 | 浏览器 | 换算 |
| --- | --- | --- | --- |
| `font_size` | px | `font-size` | 1:1（夹到 10–48） |
| `line_spacing` | 百分比（100 = 默认） | `line-height` | ×1.35 换成 CSS 倍数（Qt 的 100% 比例行距在 CSS 里约等于 1.35 倍字号）；夹到 1.1–2.8 |
| `paragraph_spacing` | px | `p { margin-bottom }` | 1:1（0–40） |
| `page_margin_chars` | 字符数 | 外层 `padding` | ×字号 换成 px |
| `first_line_indent` | 字符数 | `p { text-indent }` | 用 `em`，天然随字号 |
| `font_family` | 字体名 | `font-family` | 名字里带 宋/Serif 走衬线栈，其余走黑体栈 |
| `text_color` / `bg_color` | Qt `rgba(r,g,b,0-255)` | CSS `rgba()` | alpha 除以 255 |

行距这一条是**估算**，不是数学等价：Qt 的"100% 比例行距"落在 CSS 里没有精确对应值，先用 1.35 对齐，
真读起来别扭再调。其余都是 1:1。

### 24.8 已知不精确的地方（说清楚，不装）

- **`pos ↔ 页面` 是近似映射**。foliate 给的 `fraction` 是"按章节等权"的全局比例，而 `pos` 是字符偏移；
  两者相乘在章节长度不均的书里会差 1%～3%（一本 35 万字的书约差几页）。同一台设备来回读不会漂
  （都是同一套映射），换设备续读会落偏几页。要做准，得让桌面端把**每章字数**也传上来（二期），
  或者浏览器把所有 section 都量一遍（大书会卡）。
- **Kindle 通道没有**：老 WebKit 跑不动这套前端，二期单独做服务端渲染。
- **划线/书签不能在网页上编辑**：只读展示都没做（foliate 的 overlayer 能画，但交互和桌面端的
  坐标对齐是另一摊活）。网页上目前只同步"读到哪"和"怎么排"。
- **完整模式下的书**（有图、有复杂版式）能看，但排版设置只作用于 reflowable 文本节点；
  fixed-layout 的书 foliate 会切到固定版式渲染，字号设置对它无效——这跟桌面端的行为一致。

### 24.9 量到的数（2026-11-13，生产）

| 项目 | 数值 |
| --- | --- |
| 静态资源 | 19 个文件（foliate-js 16 + `index.html` + `app.js` 18 KB + `style.css` 5.5 KB） |
| 首屏（未登录） | 1 个 HTML（约 4 KB）+ `style.css` + `app.js` + `view.js` 及其依赖，约 **110 KB** |
| 书架 | 1 个 `GET /v1/shelf`（11 本，约 2 KB） |
| 打开一本书 | 1 个 `GET /v1/lite/<uid>/index.json` + N 个 `GET /v1/blob/<sha>`（Range 分块，3 MB 的书约 6～10 块） |
| 翻一页 | 0 个请求；**不做实时同步**——开着页面每 15 分钟、或者关页面那一秒才发（见第 25 节） |
| 章节表上传 | 9 本书首次 9 个 PUT，之后 0 |
| 桌面端重启后第一轮 | 11 个请求（GET 1 / PUT 10：9 张章节表 + 1 条进度），第二轮 0 |

### 24.10 怎么验的

| 探针 | 覆盖 | 结果 |
| --- | --- | --- |
| `cloudflare-sync/tests/worker_selftest.mjs` | Worker 全路由（内存 R2）：鉴权、状态对象、书文件、登录 Cookie、书架、章节表、Range 206、CSP | **60 / 60** |
| `tests/check_web_live2.py`-式线上验收（临时脚本） | 线上真机：`/` 是 HTML 且带 CSP、`/r/*` 静态资源、书架、Range、章节表 | **16 / 16** |
| `tests/probe_reader_ui.py` | **真浏览器**（无头 Edge + 自己写的 120 行 CDP 客户端）：登录 → 书架 → 打开一本 → 连翻 6 页读到 5075 字 → 排版注进正文（19px/25.65px/缩进 38px）→ 按键翻页 → 翻页后 3 秒云端不动 → `pagehide` 才推 → 改字号写回设置 → 目录能开能关 → 单栏/双栏、翻页/滚动切换 → 两种模式下滚轮都有效 → **刷新后版式还记得** → 无 JS 报错 | **37 / 37** |
| `tests/probe_sync_ui.py` | 桌面端同步对话框接线 + 同步节奏（15 分钟最小间隔、周期闹钟 900 秒、改动后等 870 秒才排上、`auto` 关了周期轮照样跑） | **12 / 12** |
| `tests/probe_sync.py` | 桌面端同步（含章节表上传、状态文件写盘重试） | **90 / 90** |

浏览器那条探针是这台机器上唯一能跑的"真浏览器"验证方式：没有 Chrome、没有 playwright/selenium，
于是用标准库写了个最小 CDP 客户端（`tests/mini_cdp.py`），起 `msedge --headless=new` 跑真实 DOM。
假后端 `tests/reader_mock_server.py` 提供登录/书架/进度/设置/章节表/带 Range 的书，
**不连线上、不用真 token**。

### 24.11 怎么用

1. 电脑或手机浏览器打开 `https://sync.noteklip.org/r/`（手机可以"添加到主屏幕"当 App 用）。
2. 输一次 token（桌面端「设置 → 云端同步」里那个，跟 `config.json` 的 `sync.token` 是同一个）。
   Cookie 存一年，之后不用再输。
3. 书架点一本就开始读。翻页、点屏幕左右两侧、手机上左右滑都能翻；`Aa` 里可以改成**上下滚动**、
   **单栏/双栏**，也可以调字号行距和打开目录（版式记在本机，换设备不互相影响）。
4. 位置和排版会回到桌面端；但**不是实时**：网页开着每 15 分钟同步一次、关页面时再同步一次，
   桌面端同样每 15 分钟一轮（没改动就不发 PUT）。

> 注意：`GET /` 现在回的是**阅读页**，不再是以前那行 `{"ok":true,...}` JSON。任何把它当探活接口的脚本
> 都得改到 `/v1/ping`（探针里已经改过）。

> 另一个小坑：`[assets]` 默认的 `html_handling` 会把 `/index.html` 307 到 `/`，于是用户敲 `/r/` 会被弹成 `/`
> （能用，但地址栏莫名其妙）。现在 `html_handling = "none"`，由 Worker 自己把 `/`、`/r`、`/r/` 都映射到
> `/index.html`，三个地址都直接 200，不再跳。

## 25. 阅读页第二轮：目录关不掉、翻页/滚动与分栏、同步节奏

这一轮全是"真用起来才暴露"的问题，四个都修了。

### 25.1 目录关不掉：`display:flex` 把 `[hidden]` 顶掉了

- **症状**：点「目录」能打开，点 ×、再点一次「目录」，都关不掉，只能刷新页面。
- **根因**：`.toc { display: flex }` 是类选择器，`.pane[hidden] { display: none }` 只对 `.pane` 生效；
  浏览器自带的 `[hidden] { display: none }` 优先级更低，被 `.toc` 的 `display:flex` 直接盖掉。
  JS 里 `$('#toc-panel').hidden = true` 其实一直在执行，只是渲染上没反应。
- **修法**：`style.css` 里加一条 `[hidden] { display: none !important; }`，一次把这类问题堵死
  （不再逐个元素补 `[hidden]` 规则）。
- **回归**：`probe_reader_ui` 里两条——点 × 之后 `getComputedStyle(...).display === 'none'`，再点一次按钮同样。

### 25.2 滚轮/滑动没反应：事件根本到不了外层

foliate 把正文放在 iframe 里（sandbox 允许同源脚本），**iframe 内的滚轮和点击不会冒泡到外层
`#view-wrap`**，所以原来的 `#view-wrap` 监听器只在页面留白处有效；而 foliate 自己只处理了触摸滑动
和键盘，没处理鼠标滚轮。

- 改成在 `foliate-view` 的 `load` 事件里拿 `e.detail.doc`，把 `wheel` / `click` 挂到**每一篇的文档**上
  （`WeakSet` 去重；每换一篇都会重新挂）。
- 翻页模式下滚轮阈值 240 ms 去抖（触控板一次滑动会连发几十个事件，不去抖会连翻十几页）。
- 滚动模式下滚轮的落点：folio 的滚动容器挂在 **closed shadow root** 里，`renderer.shadowRoot` 是
  `null`，拿不到 `#container`；改用它的公开 API `renderer.scrollToAnchor(fraction)`，
  `fraction = (renderer.start + dy) / renderer.viewSize`（`start`/`viewSize` 都是公开 getter）。
- 手机上的左右滑直接交给 foliate（它自己绑了 touchstart/move/end 并做 `snap`），**删掉了我们自己那份**
  ——之前两份都在，一次滑动会翻两页。

### 25.3 版式：单栏/双栏、翻页/滚动

| 选项 | 实现 | 说明 |
| --- | --- | --- |
| 左右翻 / 上下滚 | `renderer.setAttribute('flow', 'paginated' \| 'scrolled')` | foliate 原生支持，切换会重排并保持当前章节 |
| 单栏 / 双栏 | `renderer.setAttribute('max-column-count', 1 \| 2)` | 双栏只在窗口够宽时才有意义，foliate 会自己算列宽 |

- UI 放在 `Aa` 面板顶部两行，选中的按钮高亮（`.row button.on`）。
- **存在本机 `localStorage['sr.layout.v1']`，不进云端设置**：桌面端阅读窗有自己的分栏逻辑，
  把"网页双栏"塞进共用的 12 个排版键里只会让两边打架。代价是换设备要重选一次，可接受。
- 滚动模式下点屏幕不再翻页（点了就滚一下会更自然，但容易误触，索性不翻），方向键改成一屏一屏滚。

### 25.4 同步节奏：不做实时，15 分钟一次 + 关页面一次

原来是"停 2.5 秒就推"，读一小时会推几百次，手机流量和 Worker 请求都在那儿烧。

| 端 | 现在 |
| --- | --- |
| 网页 | 开着页面：`setInterval` 每 **15 分钟**一次，只有翻动过（`S.dirty`）才发；关页面 / 切后台：`pagehide` / `visibilitychange` 立刻推一次 |
| 网页（关页面那一下） | 只发**一个 PUT**，带 `keepalive: true`，`If-Match` 用打开书时记下的 ETag——这时候再 `GET` 多半来不及 |
| 桌面端 | `SYNC_MIN_GAP_SEC` 60 → **900**：改动后最快 15 分钟推一轮（连读时每一轮都落在"上一轮 + 15 分钟"，不会无限推迟）；另加 `SYNC_PULL_EVERY_SEC = 900` 的周期闹钟，没人改也每 15 分钟问一次远端 |
| 桌面端（周期这一轮） | **不受「改了就自动同步」开关影响**——那个开关管的是"一改动就催一轮"的即时同步；周期轮只按固定节拍跑，否则关了它，网页上读的进度就永远收不进来 |
| 桌面端（没改动） | 一轮里远端 rev 没变就只发 1 个 manifest；本地内容指纹没变就不 PUT（`_sync_kind` 原有逻辑） |

关页面丢最后几页的风险说清楚：`keepalive` 只保证请求发出，不保证一定送达；真丢了也不影响
——桌面端下一轮的 `merge_progress` 会按 `at` 最新的赢，位置差几页不会让书读丢。

还有一个不对称的地方（先认下来）：**网页这 15 分钟一轮只推不拉**。如果这中间桌面端把同一本书的
位置推进了，网页下一次推可能按"`at` 最新赢"把它盖回自己这边。两边都是同一个人的阅读位置，差几页
不算错，但如果你发现"网页看完回桌面反而退了"，说一声，我把"切回页面时先拉一次"加上。

### 25.5 这一轮的探针

| 探针 | 结果 |
| --- | --- |
| `tests/probe_reader_ui.py` | **37 / 37**（新增目录开关、单栏/双栏、翻页/滚动、两种模式下的滚轮、刷新后版式记得、以及"翻页后 3 秒云端不动、`pagehide` 才推"） |
| `tests/probe_sync_ui.py` | **12 / 12**（新增 15 分钟间隔、周期闹钟 900 秒、改动后等 870 秒才排上、`auto` 关掉后周期轮照跑） |
| 其它 | `probe_stats` 31/31、`probe_ui` 528/529（既有基线）、`probe_sync` 90/90、`probe_sources` 616/616 |

## 26. 网页端第三轮：设置归本机、配色/字体扩容、开始记统计

这一轮全是网页端（`cloudflare-sync/web/`），桌面端一行没动。

### 26.1 设置只在本机

原先把字号/行距这些当成"云端设置"存进 `/v1/state/settings`，和桌面端共用一份。问题是两边的排版
模型并不一样：桌面端按 Qt 的行距比例、按字符算页边距，阅读窗还有自己的分栏逻辑；网页改一次，
桌面端下一轮就把自己的值又推回去，来回打架，而且谁也不知道最后生效的是谁。

现在改成只存 `localStorage`：

| 键 | 内容 |
| --- | --- |
| `sr.settings.v1` | `font_size` / `line_spacing` / `paragraph_spacing` / `page_margin_chars` / `first_line_indent` / `face` / `paper` |
| `sr.layout.v1` | `flow`（`paginated` / `scrolled`）、`cols`（1 / 2） |

线上 `app.js` 里**已经完全不出现 `/v1/state/settings`**（读写都不碰），`tests/check_web_assets.py`
拿这条当门禁。代价说清楚：换一台设备打开网页，是默认排版——各调各的，反正一屏就调完了。

### 26.2 配色 7 档、字体 4 种

配色是"纸色 + 墨色"成对给的，不让人自己调成看不见字：

| 键 | 纸 | 墨 |
| --- | --- | --- |
| `white` | `#fbf8f2` | `#1b1b1b` |
| `sepia` | `#f4ecd8` | `#4b3f2f` |
| `green` | `#e6efd8` | `#22301d` |
| `grey` | `#eef0f2` | `#23262b` |
| `blue` | `#1b2430` | `#c9d4e2` |
| `night` | `#14161a` | `#c9c9c9` |
| `black` | `#000000` | `#a6a6a6` |

字体只排系统字体栈（`serif` 宋 / `sans` 黑 / `kai` 楷 / `round` 圆），**不加载任何网络字体**：
CSP 的 `font-src` 只放 `'self' data: blob:`，真要塞个中文字体进来至少几百 KB，翻页等不起。

### 26.3 面板里不再有说明文字

`#tools-note` 整个删了——原来那行会写"字号/行距/…都会同步给桌面端（web-…）"，存不上时还会写
"设置没存上（下次改再试）"。现在没有网络请求了，也没必要解释半天；`index.html` 里不许再出现
`tools-note`（门禁守着）。

### 26.4 网页端开始记阅读统计

原来网页只同步"读到哪"和排版，**一个字都不记**——所以在电脑上读的那几小时，账本里没有。
现在按桌面端 `reading_stats` 的口径在本机算：

| 规则 | 值 | 说明 |
| --- | --- | --- |
| 计时粒度 | 5 秒 | `visibilityState === 'visible'` 才算 |
| 空转判定 | 180 秒没有翻动 | 人可能走了，先不计时 |
| 跳转判定 | 一次往前挪 > `max(400, 1.6 × 一页字数)` | 点目录/点脚注不算读；"一页多少字"用自己最近 24 次翻页的中位数估，不用猜 |
| 一场门槛 | ≥ 60 秒 | 不足一分钟当没看 |
| 速度兜底 | ≤ 20 字/秒（1200 字/分） | 连按翻页键攒出来的数在这里被砍 |

推上去的不是日账，而是**会话**：`{k: 书 uid, title, start, end, sec, chars, gaps: []}`。身份是
`(k, start)`，桌面端 `merge_stats` 按这个身份取并集、再 `consolidate` 重算日账/书账，所以两边
读的字数会**加在一起**，而不是互相覆盖。网页这边 PUT 之前先 GET，按身份去重后整份写回（带
`if-match`，412 就放弃、留给下一轮），**从不自己重算 `days`/`books`**——那是桌面端的活。

切标签页回来会重新开一场（`visibilitychange` 里判断），否则"切出去一次"之后整段都不再计数。

书架顶上因此多了一行：`今天 40 分钟 · 15,724 字`（读的是云端账本 `days[今天]`，所以显示的是
所有设备合起来的总数）。

### 26.5 后台、幂等、不丢

这三件事得分开说，因为"记没记上"和"丢了没"是两回事。

**切到后台（挂起）**：`visibilitychange` 一变成 hidden 就把当前这一场收场（提交 + 推），
浏览器的定时器节流（Chrome 隐藏页 5 分钟后一分钟才给一次）就影响不到我们了——后台本来
也不该记时长。切回来（`visible`，或者从"往返缓存"回来触发 `pageshow` + `persisted`）立刻
按当前位置重开一场。手机上切应用再回来走的也是这条。

**幂等**：会话的身份是 `(书, 开始时刻)`，秒数不进身份。三个地方都按这个身份去重：推送前
先 GET 现有会话、`seen` 集合里有的不再追加；合并端 `merge_stats` 用同一套身份取并集；
桌面端 `consolidate` 再把同一本书 15 分钟内的多场并成一场。所以"同一场推两次"是安全的，
探针里真的重推了一次：会话 4 → 4 场、秒数 320 → 320。

**不丢**：待推队列**落在本机**（`localStorage['sr.pendingstats.v1']`，最多留 60 场），
推成功才删。后端 500、断网、`if-match` 412，会话都还在本机；下次打开网页，`enterApp`
里先补推一次。探针把后端打成 500 跑过一遍：队列里留着 1 场、云端不变；改成正常后刷新，
云端从 200 秒涨到 320 秒（+120 那一场），队列清空。

**同一浏览器开两个标签**：`sr.statowner.v1` 记"谁在记账"，只有最后活动/切到前台的那个标签
计时，另一个不记（90 秒没心跳就放开）。不这么做的话，两个标签各记一遍，同一个人一天能多出
一倍的时长。

还剩两个限度，先说清楚：

- **不足 60 秒的阅读不收**（和桌面端 `MIN_SESSION_SEC` 一致）。翻两页就关掉，那 20 秒不记账。
- `keepalive` 的 PUT 浏览器只保证"发出去"，不保证送达。以前这条会真丢数，现在有落盘队列
  兜底：发不出去的那场还在本机，下次打开补推。

### 26.6 这一轮的探针

| 探针 | 结果 |
| --- | --- |
| `tests/probe_reader_ui.py` | **45 / 45**（新增 7 个色块、4 种字体、面板无说明文字、点米黄换纸色、点楷体换字族、"只进 localStorage 不进云端设置"、刷新后配色还记得、书架统计那一行） |
| `tests/probe_web_stats.py` | **27 / 27**（新文件，四段：①真读 65 秒 → 云端多一场，书/书名/起止/字数/速度闸/原日账都对；②真切标签到后台 → 这一场立刻收场（65→135 秒），切回来接着记（135→200 秒）；③后端 500 → 队列留 1 场、云端不变，刷新后补推（200→320 秒）；④同一场重复推 → 会话数与秒数都不变；再加一段离线合并真账本：精确 +120 秒 / +2,400 字，并两次不再涨） |
| `tests/check_web_assets.py` | **20 / 20**（线上三个文件逐字节比对 + 新标记：`sr.settings.v1` 且不含 `/v1/state/settings`、`pushStats`、`sr.pendingstats.v1`、`pageshow`+`visibilitychange`、`sr.statowner.v1`、7 个色块、4 种字体、无 `#tools-note`、有 `#shelf-stats`） |
| `tests/probe_sync_ui.py` | 12 / 12 |
| 其它 | `probe_stats` 31/31、`probe_ui` 531/532（既有基线）、`probe_sync` 90/90 |
| Worker 自检 | `tests/worker_selftest.mjs` 60/60 |

线上版本：`7d21117e`（三个文件）→ `dba73f5f`（补 `visibilitychange`）→ `047d23f1`（补落盘队列、
`pageshow`、多标签互斥）。


## 27. 阅读页补四件事：能传书、有统计页、回得去、读得进去

用下来暴露的是四个具体问题，都在 `cloudflare-sync/web/` 里改：

| 问题 | 原因 | 改法 |
| --- | --- | --- |
| 阅读页「← 书架」点不动 | `#back` 是 `await pushProgress(true)` → `await flushStats()` → 才切界面。进度 PUT 一断网就抛异常，整个函数终止，界面纹丝不动 | 抽出 `goShelf()`：**先切界面**，网络推送全部改成 `.catch()` 的后台动作 |
| 读书时底栏一直占着 | `.hud` 常驻 + `#view-wrap` 留了 64px 底边距 | 底栏改成浮层，进阅读页 3.2 秒后自动收起（`body.bare`），正文吃满整屏 |
| 没有看统计的入口 | 只有书架顶上一行"今天 N 分钟" | 书架右上角加「统计」，`#stats-pane` 里放今天/累计/最近几天/每本书 |
| 网页不能自己传书 | 传书只有桌面端「设置 → 云端同步」 | 书架右上角加「上传书籍」，走 `POST /v1/blob/check` + `PUT /v1/blob/<sha256>` |

### 27.1 「← 书架」为什么点不动（这是真 bug）

老代码：

```js
$('#back').addEventListener('click', async () => {
  await pushProgress(true)          // ← 断网/401 就在这里抛出去
  await flushStats(false).catch(() => {})
  await loadShelf()                 // ← 这里也可能抛
  show('shelf-pane')                // ← 永远走不到
})
```

`pushProgress` 里的 `req()` 在 401 时 `throw new Error('unauthorized')`，`fetch` 本身在断网时
抛 `TypeError`；`pushProgress` 自己是**不**吞异常的（它是给定时器用的，吞了就没法判断成败）。
于是只要网络有那么一次不顺，点「书架」的表现就是"什么都没发生"——不是按钮坏了，是异常把
导航那行代码挡在后面了。假后端能复现：`DROP_PROGRESS_PUT = True` 让进度 PUT 直接断连，
老代码下书架永远不出现。

现在的写法是"界面先行"：

```js
function goShelf() {
  ...面板收起、statCommit()...
  show('shelf-pane')                            // 先切
  loadShelf().catch(() => {})
  pushProgress(true).catch(() => {})            // 推不上去也不拦人
  flushStats(false).catch(() => {})
}
```

顺带的好处：回书架是**立刻**的（探针里 0.00 秒），不用等一趟 HTTP。

### 27.2 沉浸：底栏是浮层，会自动收起来

- 底栏 `.hud` 从"占位"改成"浮层"：`#view-wrap` 的 `padding-bottom: 64px` 删掉，正文上下
  边距一样（探针实测 `11px|11px`）。这点很重要——栏的显隐**不能引起正文重排**，否则每收一次
  栏，分页就重算一次，页码会跳。
- 进阅读页 `barsShow(3200)`：先让人看见"怎么回去"，3.2 秒后加 `body.bare`，`.hud` 位移 130%
  + `opacity: 0` + `pointer-events: none`（收起来就真的点不到，不会误触）。
- 叫回来的三种方式：鼠标挪到底部 110px 内、按任意键、点屏幕最下面 84px 一条。前两种是桌面，
  第三种是手机（`touchstart`）；`onTapTurn` 里那一条"底部 84px 不翻页"就是为了让位给它，
  否则想点「书架」会先翻过去一页。
- `Esc`：有面板开着就关面板，没面板开着就切底栏显隐。

### 27.3 统计页：直接读云端账本

`openStats()` GET `/v1/state/stats`，渲染四块：今天（分钟）、累计（天数/分钟/字数）、最近 7 天
（逐日条）、每本书（前 12 本，带书名）。数据口径不用网页自己算——`days`/`books` 是桌面端
`consolidate` 重算出来的，所以显示的就是"所有设备合起来"的数。

一个顺手修掉的小坑：`pushStats` 原来把整个账本对象**重建**成 `{v, updated_at, device, sessions,
days, books}`，线上那份里还有 `version` 这种它不认识的键，一推就掉了。改成 `{...remote, ...}`，
不认识的键原样带回去。

### 27.4 网页传书：内容寻址，跟桌面端同一套接口

```
POST /v1/blob/check  {sha256: [sha]}        → {missing: [...], sizes: {...}}
PUT  /v1/blob/<sha256>  X-File-Name: <百分号编码的文件名>  body: 字节
```

sha256 在浏览器里用 `crypto.subtle.digest` 算，`uid = sha[:16]`。服务端已有就跳过上传（探针里
同一本传两次，书架行数不变）——这也是桌面端那条"永不删除远端、只补不改"的路子。

**总字数怎么办**：桌面端传书时顺手算了章节表（`/v1/lite/<uid>/index.json`：`total` + 每章
起始字偏移）。网页传的书没人算过，所以第一次打开时 `ensureTotal()` 自己数一遍——按 spine 逐篇
`createDocument()` 取 `textContent`，去掉空白后的长度累加，把 `total` 和每章起点写回
`/v1/lite/<uid>/index.json`，再 `pushProgress(true)` 把 `total` 和书名写进进度文档（书架上的
"读 12%"就是拿它算的）。篇数 > 400 的超大合集不逐篇解析，按"三个字节一个字"粗估，避免开一本书
卡十几秒。

**为什么不用 `sections[i].size` 当总字数**：那是 zip 里 XHTML 的**解压后字节数**，不是字数
（foliate 只拿它当分页权重）。真按字节当字数显示，中文会虚高三倍。

能传的格式对齐阅读页真能打开的：`.epub/.mobi/.azw3/.fb2/.cbz`。PDF 收不了——`vendor` 里没有
pdf.js，传上去只是书架上一行打不开的书。

### 27.5 这一轮的探针

| 探针 | 结果 |
| --- | --- |
| `tests/probe_web_upload_stats.py` | **28 / 28**（新文件：①沉浸——刚打开看得见底栏、3.4 秒后 `body.bare`、收起时 `pointer-events:none`、上下边距一样、鼠标到底部又浮出来且能点；②断网时点「书架」0.00 秒就回去了；③统计页有「统计」入口、今天的数对得上（2385 秒 → 40 分钟）、逐日行 1 行、每本书行带书名、× 能关；④上传——书架多一本、假后端按 sha 收到、原名对、打开后自己算出 2,535 字并传上章节表（3 篇）、进度文档记上 total、同一本再传不多行） |
| `tests/probe_reader_ui.py` | 45 / 45（mock 改了书架/章节表返回，回归没破） |
| `tests/probe_web_stats.py` | 27 / 27 |
| `tests/check_web_assets.py` | **30 / 30**（新增标记：`function goShelf` 且不含 `await pushProgress(true)`、`barsShow`/`body.bare`、`/v1/blob/check` + `PUT /v1/blob/`、`ensureTotal` + `/v1/lite/`、`openStats`、`body.bare .hud`、`.s-row`/`.s-book`、`#upload-btn`/`#stats-btn`、`#stats-pane`、`#file`） |

线上版本：`ab810165`（三个文件）。

线上实跑（只读）：`tests/check_live_blob.py` 4/4——`POST /v1/blob/check` 认 Bearer、本机那本
`雨` 线上已有（sha 对得上、大小 366,222 字节一致）、不带 token 是 401；再拿真 token 在真站点走
一遍（`/r/` 登录 → 11 本书 → 统计页 59 分钟 / 22,315 字 / 4 天 / 3 本书 → 打开一本 21%
（74,851 / 357,534 字）→ 3.6 秒后底栏收起、鼠标到底部又浮出来）10/10。

假后端为了这几条也加了东西（`tests/reader_mock_server.py`）：`BLOBS`/`LITE` 两个内存表、
`POST /v1/blob/check`、`PUT /v1/blob/<sha>`、`PUT /v1/lite/<uid>/index.json`、书架按 `BLOBS`
列书、未知书的章节表返回 404（否则"网页自己数总字数"这条路根本走不到）、`DROP_PROGRESS_PUT`
用来复现断网、账本里补了一条书账。


### 27.6 手机登不上：不是"只能一台"，是 http 把 Cookie 吃了

用户问的三件事，一条条查过：

**① 能同时登几台？没有限制。** Worker 里根本没有会话表：`authed()` 就是拿请求里的 token 跟
`env.SYNC_TOKEN` 做常量时间比较，Cookie 里存的就是 token 本身
（`HttpOnly; Secure; SameSite=Lax; Max-Age=31536000`）。实测三台各登一次（iPhone / Android /
Windows 的 UA），三份 Cookie **同时** `GET /v1/shelf` 都是 200 / 11 本：

```
iPhone   login → 200  Set-Cookie: 有（Secure, HttpOnly, SameSite=Lax, Path=/）
Android  login → 200  同上
Windows  login → 200  同上
三个 Cookie 同时用：/v1/shelf → 200（11 本）× 3
```

`POST /v1/logout` 只清本机那个 Cookie，不影响别的设备。

**② 手机为什么登不上——http 是元凶。** 实测 `http://sync.noteklip.org/r/` 之前**直接 200，
不跳 https**。而登录下发的 Cookie 带 `Secure`，浏览器在 http 页面上会把它丢掉（RFC6265：
非安全来源的 Secure Cookie 直接忽略）。于是现象正好是"输了 token，页面又弹回登录框"——
登录 POST 是 200 成功的，只是 Cookie 没落下。手机上很多人就是敲 `sync.noteklip.org/r/`
（浏览器补的是 http），或者从别处点开一个 http 链接。

两处都堵上：

- Worker 最前面：`url.protocol === "http:"` → **308** 到 https（308 保方法/保 body，表单和
  PUT 都能穿过去；`localhost`/`127.0.0.1` 除外，探针要跑 http）。实测现在
  `http:// → 308, location=https://sync.noteklip.org/r/`。
- 前端也兜一层：非本机地址且协议是 http 就 `location.replace('https://' + host + path)`——
  万一哪天 Worker 前面又多了个不跳转的入口。

**③ 登录失败以前一声不响。** 老代码 `await fetch('/v1/login')` 外面没有 try，断网/DNS/被拦
时 Promise 直接 reject，界面**一点反应都没有**（这也解释了"点了没反应"）。现在分四类报：

| 情况 | 提示 |
| --- | --- |
| fetch 抛异常 | 连不上同步服务（手机没网 / 网络或 DNS 不通） |
| 401 | token 不对（复制时多带了空格或换行也算不对） |
| 其它非 2xx | 服务端出错（HTTP 500），等会儿再试 |
| 登录成功但 `enterApp` 401 | 登录成功了但浏览器没存下 Cookie：用 https 打开，或关掉无痕/隐私模式 |

顺带：token 是 48 位十六进制，在手机上看不见就是一片圆点，打错一个字符根本没法查——
输入框旁边加了「显示/隐藏」；`autocomplete="off"`（别让 iOS 密码管理器塞个别的进去）、
`enterkeyhint="go"`。

**④ 顺手修了 vendor 里的一个真 bug。** 手机探针一开始报 `TypeError: undefined is not
iterable`，栈指向 `paginator.js:806 Paginator.snap`：

```js
#onTouchEnd() { ... requestAnimationFrame(() => this.snap(vx, vy)) }   // 分页模式也调
snap(vx, vy) { const [offset, a, b] = this.#scrollBounds }             // 分页模式下它是 undefined
```

`#scrollBounds` 只在 `#scrollTo()` 里赋值，分页模式永远走不到那里，所以**手机上每点一下屏幕
就抛一次异常**。改法（`vendor/foliate-js/paginator.js`，标了"本地补丁"）：`snap()` 开头
`if (!this.scrolled) return`，并给 `#touchState` 加空判。`check_web_assets` 会把
`paginator.js` 也逐字节比对，并断言这个补丁在线上。

**⑤ token 能改。** 它就是一个 Worker secret：

```
cd cloudflare-sync
npx wrangler secret put SYNC_TOKEN     # 粘贴新 token，回车
```

改完的代价是**所有设备一起失效**（Cookie 里存的就是 token，桌面端的 Bearer 也是它）：
每台浏览器重新输一次，桌面端 `config.json` 的 `sync.token` 跟着改（改完不用重启也能生效，
下轮同步自己就用新值）。要换我建议一次换到位：长度 ≥16 位随机串就够（对比是常量时间、
又只走 https），不用非得 48 位——手机上少打一半字符。

### 27.7 这一节的探针

| 探针 | 结果 |
| --- | --- |
| `tests/probe_web_mobile.py` | **21 / 21**（新文件：390×844 + `mobile=true` + 真触摸 + iPhone UA——登录框放得下 / 「进去」在屏幕里 / 44px 高 / token 默认遮住、点「显示」变 text 再点遮回去；**断网报"连不上"、错 token 报"token 不对"、报完按钮没被禁用卡死**；书架标题栏不溢出（390 ≤ 390）、三个按钮都在屏幕里；统计页不横向溢出且有书；真触摸点右边翻页（0 → 85 字）、点底部把底栏摸出来、点「← 书架」回得去、整轮无 JS 报错） |
| `tests/probe_reader_ui.py` | 45 / 45（登录标记改了，回归没破） |
| `tests/probe_web_upload_stats.py` | 28 / 28 |
| `tests/probe_web_stats.py` | 27 / 27 |
| `tests/check_web_assets.py` | **32 / 32**（多了 `paginator.js` 的逐字节比对 + 补丁断言） |
| 线上只读诊断 | 三份 Cookie 并存 3/3、`http:// → 308`、错 token 401、无 Cookie 401 |

线上版本：`23242a55`（http→https）→ `74e7308f`（「显示」按钮、登录报错）。

### 27.8 网页打开一本书，到底下不下第二次

**一句话：第一次一定整本下一次；24 小时内再开同一本，一个字节都不下；超 24 小时只问一句 etag
（304），也不再整本重下。** 但我们没有自己做离线缓存——靠的是浏览器自己的 HTTP 缓存。

先说清现在的实现（`vendor/foliate-js/view.js: fetchFile`）：

```js
const fetchFile = async url => {
    const res = await fetch(url)                  // 一次 GET，不发 Range
    return new File([await res.blob()], ...)      // 整本进内存，再交给 zip.js 按需解
}
```

所以：**没有任何 Cache API / IndexedDB / Service Worker**，每次 `openBook` 都会发一次
`GET /v1/blob/<sha>`；整本书（不是按章）一次拿完，解压后留在内存里。

能省下流量的是 Worker 给这个响应打的两个头（`handleBlobGet`）：

```
cache-control: private, max-age=86400     ← 24 小时内浏览器直接用磁盘缓存，不问服务器
etag: <R2 的 etag>                        ← 过了 24 小时拿它来问"变了吗"
```

书是**内容寻址**的（文件名就是 sha256），内容永不变，所以"缓存 24 小时""etag 不变就 304"
在这里是安全的，不需要任何失效逻辑。

实测（真站点、真书、headless Edge，读 Resource Timing 的 `transferSize`）：

| 动作 | 走网络 | 耗时 |
| --- | --- | --- |
| 第一次打开《动物农场》（1,250,479 字节） | **1,250,779 字节**（含响应头） | 8.9 秒 |
| 刷新页面、再打开同一本 | **0 字节** | 2 毫秒（磁盘缓存） |

**这一轮补的那一半**：以前过了 24 小时，浏览器带着 `If-None-Match` 来问，Worker 压根不看这个
头，照样把整本再塞一遍（一本 1.2 MB 的书每次白下）。现在对上了就回 `304` + 空 body：

| 请求 | 结果 |
| --- | --- |
| `GET /v1/blob/<sha>` | 200，8,485,601 字节 |
| 同上 + `If-None-Match: <etag>` | **304，0 字节** |
| 同上 + `If-None-Match: "<etag>"`（浏览器会加引号） | 304 |
| 同上 + `Range: bytes=0-9` + `If-None-Match` | 206，10 字节（要部分就给部分，不走 304） |

什么时候真的会重下：清掉站点数据 / 无痕窗口 / 换浏览器或换设备 / 手机系统回收了 Safari 的磁盘
缓存。**没有离线能力**（断网打不开书），要离线得加 Service Worker，那是计划里的二期。
桌面端是另一套：它把书拉到本地磁盘长期留着，断网照样读。

`worker_selftest.mjs` 因此从 60 项变成 **65 / 65**（etag/缓存头、304 空 body、引号 etag、
etag 不符仍是 200、带 Range 不走 304）。
