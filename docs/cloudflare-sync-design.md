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
结果缓存进 `config.json` 的 `local_book_uid`：

```json
"local_book_uid": {
  "c:\\users\\kso\\downloads\\雨 (黄锦树)...epub": {"uid": "3f9a1c2b7d4e5f60", "size": 366182, "mtime": 1762...}
}
```

规则：
- 缓存命中条件 `size` + `mtime` 都没变（改一个字节就重算）；算一次 8 MB 的 epub 约 20 ms，9 本一次性 < 0.2 s，
  放在后台线程里做，界面不等。
- 同一本书在两台机器上路径不同但内容相同 → UID 相同 → 进度/笔记天然对上。
- 老数据迁移：第一次启用同步时，把已有的 `local_book_positions` / `annotations` 按本机路径算出 UID，
  生成"UID 视图"再上传；本机原有的路径键**不动**（现有代码一行不改，同步只在旁边读）。

### 5.2 四个同步对象（都是 JSON，UTF-8，不带 BOM）

**progress**（进度，按 UID）
```json
{"v": 1, "updated_at": 1762950000.123, "device": "home-pc",
 "books": {"3f9a1c2b7d4e5f60": {"title": "雨", "pos": 183631, "total": 366182,
                                "at": 1762949990.5, "dev": "office-nb"}}}
```

**annotations**（划线/书签，按 UID；`id` 里的毫秒数就是该项的时钟）
```json
{"v": 1, "updated_at": 1762950000.1, "device": "home-pc",
 "books": {"3f9a1c2b7d4e5f60": {
   "highlights": [{"id": "h1762949000123", "start": 1024, "end": 1088, "text": "…",
                   "note": "", "at": 1762949000.123}],
   "bookmarks":  [{"id": "b1762949100456", "pos": 4096, "snippet": "…", "at": 1762949100.456}],
   "tombstones": [{"id": "h1762948000000", "at": 1762949500.0}]}}}
```
> 兼容老数据：老条目没有 `at`，从 `id` 里抠毫秒（`b1762949100456` → `1762949100.456`），抠不出来当 `0`。

**stats**（账本：只传 `sessions`，`days`/`books` 收到后本地 `consolidate()` 重算）
```json
{"v": 1, "updated_at": 1762950000.2, "device": "home-pc",
 "sessions": [{"k": "3f9a1c2b7d4e5f60", "title": "雨", "start": 1762945200.0,
               "sec": 905.0, "chars": 5127.0, "end": 1762946105.0, "gaps": [[600, 120]]}]}
```
> `k` 改成 UID 后，两台机器的同一本书才会并成"一本书"。老账本里的路径键在合并时按 5.1 的映射换掉，
> 换不出来的（书已经不在了）原样保留。

**settings**（只同步"跨机器有意义"的键）
- 同步：`font_size font_family line_spacing paragraph_spacing page_margin_chars first_line_indent
  paragraph_indent page_overlap_lines arrow_scroll_lines text_color bg_color opacity lowkey_key
  peek_key boss_key solid_key safe_key anki_deck anki_swallow_keys anki_conceal feed_lowkey
  feed_type_scheme auto_bg_solid ghost_mode antishot_mode restart_on_unlock`
- 不同步（设备本地）：`window_width window_height last_local_file last_local_pos safe_doc_path
  local_book_positions local_book_totals local_book_times local_book_hidden local_book_uid`
- `local_book_hidden`（移出书架）单独按 UID 并集进 progress，不进 settings。

### 5.3 本地 `sync_state.json`（新增，不进 git）

```json
{"device_id": "9f2c1a...", "device_name": "home-pc",
 "kinds": {"progress": {"rev": "d41d8cd9", "sha256": "…", "pushed_at": 1762950000.5, "dirty": false},
           "annotations": {...}, "stats": {...}, "settings": {...}},
 "pending": [], "last_error": "", "last_ok_at": 1762950000.5}
```

## 6. 同步协议（7 个端点，够用且好测）

所有请求：`Authorization: Bearer <token>`；`Content-Type: application/json`；只走 HTTPS。
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
| 账本 | `sessions` 按 `(round(start), k, round(sec*1000), round(chars))` 去重取并集，然后 `consolidate()` 重算 `days`/`books`。并集天然可交换/可结合，两台机器怎么并都是同一个结果 |
| 设置 | 文件级 LWW（整份，靠 `updated_at`）；白名单外的键一律不动 |
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

## 8. 客户端怎么落在这个仓库里

新文件 `sync_engine.py`（约 700–900 行，标准库，跟 `reading_stats.py` 一个风格：纯函数 + 一个线程类），
`main.py` 只加接线：

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
  "interval_sec": 600,
  "last_ok_at": 0,
  "last_error": ""
}
```
`device_id` 不进 config（属于"这份安装"），放 `sync_state.json`。

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
| 本地文件同时被写 | 读之前先 `os.stat` 两次（mtime+size 稳定才算），读到的写回前再比一次；不一致就下轮 |
| 时钟偏差（两台差几分钟） | 字段级 LWW 用各自本地时间，误差可接受；同一个字段两边都改且相差 < 1 分钟时，可能选错一边——概率极低，接受 |
| 两个进程同时跑同步 | `sync_state.json.lock`（`os.open(..., O_CREAT|O_EXCL)`）+ 30 秒过期，拿不到锁就跳过这轮 |
| 书文件传到一半断了 | 内容寻址天然可重传（同一 sha256 重传，R2 覆盖无害）；`blob/check` 先问缺哪些 |
| 磁盘满了 / 写不进去 | 写回失败 → 本地保持旧文件（原子替换保证），日志报错，同步暂停到下次启动 |

## 13. 安全与隐私

- 传输只有 HTTPS；Worker 只认 Bearer token（`wrangler secret put SYNC_TOKEN`，不进仓库）。
- 服务端能读到明文（你的书页摘录、笔记）。**如果这不能接受**，二期加端到端加密：
  口令 → PBKDF2 → AES-256-GCM 加密后再上传，服务端只见密文；因为合并本来就在客户端做，加了加密不影响合并。
- R2 bucket 不公开（`r2.dev` 关闭），只能过 Worker。
- token 泄露的最坏结果：别人能读写你这一桶书摘 —— 所以 token 用 32 字节随机串，换 token＝在 Worker 里
  `secret put` 覆盖，客户端改一处即可。

## 14. 中国大陆可达性（先说清楚，别到时候白忙）

- **`*.workers.dev` 在国内基本不通**，别用它。必须挂**自定义域名**（Cloudflare 代理，走 CF 边缘）。
- 挂了自定义域名也**只是"通常能通"**，不是保证：延迟常在 150–400 ms，偶尔被干扰。
  所以：① 同步全部走后台 + 超时短 + 退避，不通就是"离线"，不影响读书；
  ② 客户端只认 `base_url`，真不行换掉（公司/家里的服务器、坚果云 WebDAV 网关都行）；
  ③ 设置页放「连通性自检」，直接量出 DNS / TLS / ping / 一次 PUT 的耗时，别靠感觉。
- 书文件（19 MB）在国内链路上首次上传可能要几分钟，所以默认**不同步书文件**；打开时显示预计流量。

## 15. 分三期，每期都能单独验收

**一期（最小可用，约 1–1.5 天）**
- Worker + R2 + 四个 kind 的拉/推/合并 + 设置页 + 托盘 + 首次同步对话框
- 验收：两台机器（或同一台换 `APP_DIR` 模拟）→ 进度/笔记/账本互相同步；拔网线 → 不报错、恢复后自动并。

**二期（备份与恢复）**
- 书文件同步（内容寻址、先 check 再传）、历史版本页签（5 个旧版本一键回滚）、端到端加密开关
- 验收：删掉本地 `annotations.json` → 一键从远端恢复；改坏远端某对象 → 回滚到上一版。

**三期（省心）**
- 冲突可视化（哪本书、哪个字段冲突，并排给你看）、同步日志页、按书选择性同步
- 验收：故意制造冲突（两台同时改同一本书的进度）→ 界面能说清"并了什么"。

## 16. 测试计划（不部署 CF 也能验 90%）

1. **纯函数（`probe_sources` 里加约 24 条）**：四个 merge 函数 + UID 迁移 + 墓碑 + 时间戳解析
   （老 `id` 抠毫秒、老 `created` 字符串）+ `consolidate` 幂等（并两次 == 并一次）。
2. **假后端端到端（`tests/fake_sync_server.py`，`http.server` 一个内存实现，含 ETag/412/401/超时/断线开关）**：
   首传、拉取、真冲突、离线重试、token 错、书文件去重、远端坏 JSON。
3. **真机自检 CLI**：`python sync_engine.py --selftest <base_url> <token>` → ping / PUT / GET / 412 / blob 五步，
   打印每步耗时，部署完先跑这个。
4. **实机**：两台机器（或两个 `APP_DIR`）轮流读同一本书，看进度是不是"后读的赢"、笔记是不是"两边都在"。

## 17. 部署清单（你只需要做 3 件事，其余我写脚本）

1. `wrangler login`（浏览器授权一次）；
2. `wrangler r2 bucket create stealthreader-sync`；
3. 给 Worker 绑一个**你自己的子域名**（比如 `sync.yourdomain.com`），并在设置页填 `https://sync.yourdomain.com` + token。

其余（`wrangler.toml`、`worker.js`、`secret put`、自检）都在 `cloudflare-sync/README.md` 里按步骤做。

## 18. 我替你做的决定（不同意就说，改起来都很便宜）

| # | 决定 | 为什么 |
|---|---|---|
| 1 | 默认**不同步书文件**，只同步数据 | 19 MB 在国内链路上首次要几分钟；数据和书可以分开决定 |
| 2 | 书身份用**内容 sha256 前 16 位**，不用路径 | 路径换机器必废；内容寻址顺带让书文件去重 |
| 3 | 账本只传 `sessions`，收到后 `consolidate()` 重算 | 单一真相源，合并天然幂等 |
| 4 | 冲突一律**客户端合并**，服务端只做条件写 | Worker 保持 220 行的"笨"，好审计、好测 |
| 5 | 删除只带墓碑，**远端永不删对象/书** | 你的底线：数据只能变多不能变少 |
| 6 | 同步对象是"一个 kind 一整份 JSON"，不是逐条 | 单人 + 5–8 KB，整份最省事；逐条同步是给协作用的 |
| 7 | 不做后台常驻，跟 app 生命周期 | 少一个守护进程，少一类 bug |
| 8 | 设计文档放在 `docs/cloudflare-sync-design.md` | 跟现有两份设计文档同一层，不另起 `specs/` 目录 |

## 19. 留给你的四件事（有默认值，不回也能开工）

1. **Cloudflare**：有账号吗？有一个能挂子域名的域吗？（没有的话：先不绑域名，用 `workers.dev` 只做验证，
   真日用再换；或者我直接把后端做成"任意 HTTP 端点"，先接你公司/家里现成的东西。）
2. **书文件同步**：要不要？（默认不要）
3. **几台机器、都 Windows 吗**？（影响路径规范化与冲突策略的细节，默认"两台 Windows"）
4. **明文存云端接受吗**？（默认接受；不能接受就把二期的 E2E 提到一期，晚 1 天）

## 20. 风险登记

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| 国内访问 CF 不稳 | 中 | 高 | 后台 + 退避 + 可换 endpoint + 自检 |
| 合并逻辑出错导致丢笔记 | 低 | 高 | 只并集 + 墓碑 + 写前 `.bak` + 纯函数 24 条单测 + 假后端端到端 |
| 首次同步把本地覆盖 | 低 | 极高 | 不提供"覆盖本地"选项；首次只合并 + 显示将新增什么 |
| 书 UID 计算拖慢启动 | 低 | 低 | 后台线程 + size/mtime 缓存 |
| 你换机器后书路径变了 | 中 | 低 | 进度按 UID 走；书架靠重新添加文件再按内容认回来 |

---

**下一步**：这份设计过了，就按它出实施计划（`superpowers:writing-plans`），然后一期动手。
