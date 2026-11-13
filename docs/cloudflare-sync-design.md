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
