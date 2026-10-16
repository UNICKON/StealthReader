# 隐形背诵 — 分批实施计划

配套设计：`docs/anki-review-design.md`。原则：**复用 > 复制**，每批结束都要能单独验证，
批次 1 之后所有 UI 工作都能用桩数据（不连 Anki）验证。

---

## 批次 0：前置（你来做，30 秒）

Anki → 工具 → 插件 → 获取插件 → 填 `2055492159` → 重启 Anki。
我做探针验证：`version` 返回 6、`deckNames` 返回你那 15 个牌组。
若插件装不上/加载失败 → 转设计文档第 8 节的 B 方案（只读展示，评分写不回）。

## 批次 1：`anki_client.py`（不碰 UI，命令行可验）

新文件 `StealthReader/anki_client.py`，约 160 行：

- `AnkiError` / `AnkiUnavailable`（连不上）/ `AnkiNoCard`（Anki 说没卡了）
- `AnkiClient.invoke(action, **params)`：POST JSON-RPC v6，`error` 非空即抛；
  连不上（`ConnectionRefused`/超时）转成 `AnkiUnavailable`
- `ping()` / `version()`；`deck_names()`
- `start_review(deck)` = `guiDeckReview`；`leave_review()` = `guiDeckBrowser`
- `current_card()` = `guiCurrentCard`（把"GUI 不在复习界面"与"今日已清"区分成两种可识别错误）
- `show_answer()` = `guiShowAnswer`；`show_question()` = `guiShowQuestion`
- `answer(ease)` = `guiAnswerCard`；`undo()` = `guiUndo`
- `due_count(deck)` = `len(findCards('deck:"X" is:due'))`
- `media_path(name)`：定位 `%APPDATA%\Anki2\<配置档>\collection.media`（多档取最近修改的），
  找不到再退 `retrieveMediaFile` 的 base64
- `conceal_anki()` / `reveal_anki()`：ctypes `EnumWindows` + `GetWindowThreadProcessId` 按 Anki 的
  pid 找顶层窗口，`ShowWindow(SW_HIDE / SW_SHOWNOACTIVATE)` —— **藏起来而不是最小化**，
  还原也绝不激活（见设计 §3.6 第 1 条）

**验证**（一次性探针 `%TEMP%\anki_probe.py`，用完删）：version / deckNames / deck_stats /
current_card（只看长度）/ media_path 命中率 / conceal+reveal 往返。**探针绝不调用 `answer`，
也不打印任何卡片内容。**

**状态：已完成，两轮探针全绿（24/24 + 评审核实 14/14）**。接口契约按真机实测改了一遍，详见设计
文档「3.5 本机实测校正」与「3.6 评审后的接口契约修正」：
- 实参一律嵌 `params`（扁平写法被静默忽略）；信封是 AnkiConnect 自有格式，**不是 JSON-RPC 2.0**
- 显式禁用代理（本机代理插手 localhost：未监听端口回 502，且卡片内容不该过第三方）
- `anki_hwnd` 按 Qt 的 `QWindowIcon` 类名 + `GetWindowPlacement` 的还原矩形挑主窗口
- 错误多分两类：`AnkiBusy`（Anki 正在同步等，头号风险）、`AnkiNeedsKey`（配了 apiKey）；
  `gui*` 返回 `False` 也算失败（`start_review` 遇到不存在的牌组就是返回 False）
- 用 `getMediaDirPath` 取代自己扫目录；`guiPlayAudio` / `guiStartCardTimer` 取代自建音频与计时；
  `answer_and_next` 走 `multi`（评分是异步 accepted）；计数用 `getDeckStats` / `getNumCardsReviewedToday`
- 藏 Anki 用 `SW_HIDE`：实测之后 GUI API 照常可用，且不进 Alt+Tab、没有任务栏缩略图

## 批次 2：`ReviewMode` 进出 + 卡片渲染（用桩卡，不需要 Anki）

`main.py`：

- **抽公用函数**：`snapshot_book_state()` / `restore_book_state(snap)`（从现有
  `enter_safe_mode` / `exit_safe_mode` 里原样搬出来，安全文档与背诵共用）
- 新增 `self.review_mode = False`、`self.review_session = None`（牌组 / cardId / 是否翻面 / 已背数）
- `enter_review_mode()`：若在安全文档先 `exit_safe_mode()`；`snapshot_book_state()`；
  `client.start_review(deck)`；`render_card()`；`start_card_timer()`；`conceal_anki()`；
  `refresh_hotkeys()`
- `exit_review_mode(restore=True)`：卸复习键 → `leave_review()` → `reveal_anki()`（**不激活**）→
  `restore_book_state()` → 重绘书页（精确到字）
- `render_card(card)`：`simplify_anki_html()`（剥掉整块 `<style>` **和卡片里的内联
  `font-size`/`<font>`**、抽走 `[sound:x.mp3]`、`class="cloze"` → 加粗）→ 过滤后的 `card["css"]`
  塞 `document().setDefaultStyleSheet()`（保留颜色/居中/表格，卡片才像文档）→
  `document().setBaseUrl(collection.media)` → `setHtml()` → `apply_card_style()` → 只留白不收边
  → `verticalScrollBar().setValue(0)`
- **卡型白名单**：纯文本 / 简单图片 / cloze / 音标卡正常渲染；遇到 JS、Image Occlusion、
  打字作答、MathJax 这类渲染不了的卡型，明确提示"该卡型不支持隐形显示"——**不给空白**
  （空白比走样更可疑）
- **翻面状态自己记**（`self.review_shown`）：实测 `guiCurrentCard` 的 `answer` 翻面前就已渲染好，
  不能靠它判断当前该显示哪面
- `apply_card_style()`：居中、不首行缩进、不两端对齐、正常行距，字号沿用阅读字号；卡片自带的
  `@font-face`（你那 3 个 otf）不加载，用阅读器字体渲染
- **守卫改成一个判定**：新增 `_book_veiled() = safe_mode or review_mode`，替换 `main.py` 里那
  16 处 `safe_mode` 判断（`record_view_position` / `repage_to` / `scroll_page` /
  `apply_page_margins` / 滚轮分支 / 进度写入等）。其中两处**会损坏数据**，必须覆盖：
  `apply_highlight_selections`（3339，否则书页高亮画到卡片上）与 viewport 鼠标命中
  （3654，否则卡片文字会被按卡片偏移写进这本书的划线存档）
- 抽公用快照函数时要加 `write_progress=False`：现有 `exit_safe_mode` 会写
  `config["last_local_pos"]`（main.py:2917 一带），背诵这条路**不许写**，探针要断言 config 未变
- `apply_style()` 加分支：背诵中改字号/字体 → 重渲染当前卡

**验证**（`%TEMP%\m14_review_ui.py`，桩 client + fixture 卡）：进入后正文=卡片、书的状态
（`local_start_index` / `last_local_pos` / 视口顶锚点）一字未变、滚轮滚卡片不翻页、改字号重渲染、
退出后 `current_top_offset()` 与进入前一致。

## 批次 3：键位吞键 + 评分回路（桩 client 可验）

- 扩展现有 `GlobalInputHook`：加 `install_review(keys)`；`_keyboard_proc` 里
  **只有 `owner.review_mode and 窗口可见且未最小化` 才吞**（隐藏即放行，不必重挂钩子）；
  `Space/Enter/1/2/3/4/U/R/Esc` 交给 `on_review_key(name)`，其余一律放行；
  加设置开关 `anki_swallow_keys`（默认开），关掉就完全不吞
- `on_review_key()`：空格/回车 → `show_answer()` 渲染反面 + `play_audio()`；
  **评分键按 `card["buttons"]` 映射**（学习步/FSRS 下可能只有 2 个按钮，按到没提供的那个
  `guiAnswerCard` 会返回 False）→ **先核对 `cardId` 与屏幕上是同一张**（不一致就重渲染、
  这一下不评分）→ `answer_and_next(ease, prev_id)`（内部走 `multi` 等最终态）→ 渲染新卡 +
  `start_card_timer()`；评分为 False 时**不前进**，提示"这张卡没这个按钮"；
  `U` → `undo()` + 重取；`R` → `play_audio()`；`Esc` → `exit_review_mode()`
- **不做自建音频**：播放交给 `guiPlayAudio()`（`[sound:]`、`{{tts:}}`、自动播放策略它自己懂），
  我们只负责把标签从显示文本里删掉
- 状态行：`牌组 · 待复习 N（Anki 口径）· 今日已复习 M`，全部取 `getDeckStats` /
  `getNumCardsReviewedToday`；`review_active()` 为 False 且 `deck_stats` 已清 → "今日已清"
- **busy 分支（头号风险）**：`AnkiBusy` 时状态行显示"Anki 正在同步…"，退避重试（1s/2s/4s），
  **绝不静默吞掉用户的评分**，也绝不因为忙就退出背诵
- **对账**：窗口可见时每 2–3 秒核对一次 `guiCurrentCard().cardId`，与屏幕上的不一致
  （用户跑去 Anki 自己窗口答了卡、或插件 veto）就重渲染并提示
- 切配置档 / apiKey 缺失：优雅退出背诵并在状态行说明，不抛原始异常
- **`refresh_hotkeys()` / `recover_hotkeys()` 之后，若在背诵且可见 → 重挂复习键**
  （否则看门狗每 90 秒的静默重挂会把吞键键位清掉）
- 全程：卡片正文/答案/音频名**不进日志、不进探针输出**

**验证**（`%TEMP%\m15_review_keys.py`，桩 client）：9 个键各自行为、隐藏时不吞、退出后不吞、
`buttons` 只有 2 个时按 4 不前进且有提示、cardId 不符时不评分、busy 时不吞评分且会重试、
Anki 抛错时提示并退回书页、日志里不含卡片内容。

## 批次 4：入口 / 设置 / 菜单 / 文档

- 热键 `ctrl+alt+a` 进 `refresh_hotkeys()`（沿用重复键校验），`on_review_hotkey_triggered`
  包 try；`recover_hotkeys` 一并覆盖
- 设置对话框加"背诵"区：开关热键、牌组（列表来自 `deckNames`）、吞键开关 `anki_swallow_keys`
  （默认开）、藏 Anki 开关（默认开）
- 托盘菜单 + 右键菜单加"开始背诵 / 退出背诵"
- 首次进入且没有牌组配置 → 弹牌组列表选择（带每副牌组的 `待复习 N`，来自一次 `getDeckStats`）
- README 加"隐形背诵"一节（键位表 + 数据边界 + 与 cd/ce/cad 的关系 + **"藏起来≠隐身"的边界说明**：
  窗口一旦显示、书页一旦翻到，屏幕上就是可见的）

## 批次 4.5：跨模式切换（设计 §5 那四条承诺，原计划漏了）

四条都要实现并逐条写成探针断言：

| 场景 | 期望 |
|---|---|
| 背诵中 `Ctrl+D` | 隐藏窗口，**不退出背诵**；叫回来还是**同一张卡**（不重取、不重新排程） |
| 背诵中 `Ctrl+Alt+D` | 换安全文档；再按一次回来**还是同一张卡**（重新渲染同一 cardId，不评分） |
| 背诵中 `Ctrl+E` | 先退出背诵回书，再浮现书页偷瞄 |
| 安全文档中 `Ctrl+Alt+A` | 先退出安全文档回书，再进背诵 |

实现要点：背诵会话（牌组 / cardId / 是否翻面 / 本次已背）活在内存里，安全文档的快照/还原不动它；
退出安全文档时若会话还在，回到卡片而不是书页。

## 批次 5：打磨（可选，先不做也行）

`Q` 回正面、长答案滚动条外观、音频自动播放开关、Anki 中途退出后的自动重连。

## 每批的收尾动作（固定）

1. `py_compile main.py`（+ 新模块）
2. 本批探针全绿
3. 回归：进度/锚点、安全文档、页边距、老板键四条老探针全绿
4. 探针文件用后即删（`%TEMP%` 不留东西）
5. 全部批次完成后重启 live 实例、更新 README、`git commit`（作者 `UNICKON`）、推 `myfork`

## 规模

批次 1 约 160 行、批次 2 约 250 行、批次 3 约 150 行、批次 4 约 120 行；
主体是 1–2，先落这两批就能真机试用。

---

## 完成情况（全部批次已落地）

| 批次 | 状态 | 验证 |
| --- | --- | --- |
| 0 插件 | 完成 | AnkiConnect 已装（代码 `2055492159`），Anki 26.9.3 |
| 1 `anki_client.py` | 完成 | 两轮真机探针 35/35、14/14 |
| 2 `ReviewMode` + 渲染 + 守卫 | 完成 | 探针 49/49（含四条老路径回归） |
| 3 吞键 + 评分回路 | 完成 | 探针 24/24 |
| 4 入口 / 设置 / 菜单 | 完成 | 探针 27/27（与 4.5 同一次） |
| 4.5 跨模式切换 | 完成 | 同上一行，四条表逐条验过 |
| 4.6 卡顿治理 + 页脚信息条 | 完成 | 探针 54/54、回归 31/31、真机 19/19、几何 12/12 |
| 4.7 键捕获分层 + 偷瞄正交 + 缩放 + 页脚可读性 | 完成 | 功能探针 48/48、书页回归 28/28（SRS 零改动：日志只有主人自己那两次评分） |
| 5 打磨 | 未做（按计划可选） | — |

### 批次 4.6（真人试用后的两条反馈：卡顿 + 页脚）

> 同批还试过一版"键位分工"（`Ctrl+D` 只在窗口前台或鼠标在窗口上时生效、偷瞄键
> `suppress=True` 一个键都不漏给别的软件）。**已按要求撤回**，老板键回复全局无条件生效、
> 偷瞄键不再 suppress；相关的 `boss_key_actionable()`、win32 句柄声明与 README 段落一并删掉。

1. **交互卡** → 量出来：每个 `gui*` 动作固定 ~60ms（AnkiConnect 要把它丢回 Anki 主线程），
   原来对账每 2.5 秒在主线程同步打一次，进牌组那串调用更是一口气堵 0.7 秒。
   现在所有 Anki 调用走一条常驻后台线程（队列串行，保证评分顺序），结果用信号回主线程：
   进背诵 19ms 返回、每 2.5 秒的对账在主线程只花 0.06ms、翻面 0ms。对账带去重（忙时不堆队），
   媒体目录进牌组时取一次就缓存（原来每次换卡都连一趟）。
   顺带修掉一个真隐患：`closeEvent`（X 关窗，最常用的退出方式）原来不停后台线程，
   PyQt 会打印 `QThread: Destroyed while thread is still running` 并可能崩；
   现在 `closeEvent` / `quit_app` / `aboutToQuit` 三处都停，且退出前**同步**把 Anki
   从"复习器 + 藏起来的窗口"里放出来（异步任务在停线程时会被丢掉，Anki 会一直找不着）。
2. **显示学习计划 / 按钮 / 影响结果** → 正文下面一条页脚（小一号、淡一点，像文档底下的一行小字）：
   `新 20 · 学 4 · 复 159　今日已 12　本次 3` + 四个按钮，按钮文字带 Anki 的 `nextReviews`
   （`3 良好 3 天`）——这就是按下去对这张卡的影响；翻面前按钮是灰的（和 Anki 一致），
   翻面后亮起，卡上只有 2 个按钮时多的收掉，安全文档盖着时整条页脚（连按钮）一起消失。


真机冒烟（只看不答，SRS 未改动）12/12：真卡正面 2124 字符 → 简化后 141 字符、
答案 2377 → 394，正面渲染不含答案正文，`guiDeckBrowser` 正常退回，Anki 窗口未受影响。

### 批次 4.7（试用后的三条反馈：键捕获是全局的 / 缩放就出错 / 页脚字看不清）

主人的原话把模型定死了：**"阅读页和 anki 页是不同的标签页，但其他交互逻辑应该一样"**。
也就是"内容层（书页 / 卡片 / 安全文档）"和"可见层（Ctrl+D 收起 / Ctrl+E 偷瞄 / 抗拍 / 透明度 / 排版）"
是两件事，这一批就是把这四个字落实到代码里。

1. **交互键的捕获改成"按 cd / ce 分"**（反馈 1「anki 模式下的交互键捕获是全局的」）：
   先分清两件事——**快捷键**（`Ctrl+D`/`Ctrl+E`/`Ctrl+Alt+D`/`Ctrl+Alt+A`）和
   **卡片交互键**（`空格/回车/1-4/U/R/Esc`）。主人说的"全局"指的是后者。
   - 四个快捷键**保持全局热键**（中途我一度把它们改成窗口内 `QShortcut`，被否掉，已恢复；
     `QShortcut` 只在全局注册失败时兜底，两条路不并存——并存时窗口是前台那一次会触发两遍，
     老板键会收起又立刻弹出）。
   - 交互键的归属由可见层决定：`Ctrl+D` 收起 → 不吞；普通显示 + 窗口是前台 →
     窗口内 `keyPressEvent`；普通显示但窗口不在前台 → 不捕获；`Ctrl+E` 偷瞄 → 全局捕获
     （`peek_captures_review_keys()`）。低级钩子**只在偷瞄期间安装**。
   - 旧实现的 `review_keys_active()` **根本不看焦点**（只要"背诵中 + 窗口可见 + 没最小化"），
     窗口摆在屏幕角落、人在别的软件里打字，空格和 `1-4` 会被卡片吃掉。
   - 收尾：没翻面时按 `1-4` 不再评分（原来只查 card/buttons，而偷瞄那层全局可达，
     手滑就改排程）——与页脚灰按钮同一个门，探针实测这一下一个 Anki 请求都不发。
2. **偷瞄与内容层正交**（同一句"不同的标签页"的那个"但是"）：
   删掉 `toggle_peek()` 里"先退出背诵 / 先退出安全文档"两个前奏和 `exit_peek()` 里的安全文档分支。
   现在背诵中按 `Ctrl+E` 是**原地偷瞄卡片**（同一张卡、同一进度），退出偷瞄只隐藏窗口；
   偷瞄中按 `Ctrl+Alt+D` 换成安全文档时**偷瞄保持**。滚轮在卡片页只滚卡片，不碰书的页首。
3. **窗口缩放**（反馈 2「缩放就会出错」）：量出来是两条——卡片页借用了书页"退到最后一个完整行"
   的底部裁切（130px 的短卡片被算出 261px 下边距，视口只剩 130px，窗口一缩小**掉到 0**），
   以及拖动缩放时根本没重排（`repage_to` 在卡片页一进门就返回）。现在卡片页用固定四边页边距、
   拖动期间 0.05 秒节流重排、松手补尾。实测 707 → 372 视口，内容底跟着新高度走。
4. **页脚可读性**（反馈 3「下方的字太小而且看不清」）：
   卡片正文字号实测是 14pt 统一的（不是字体问题），问题在页脚——11px + 0.61 不透明度。
   现在字号跟设置走（默认 16px）、不透明度 0.92、加同色薄衬与按钮边框、两行布局、
   剔掉 `nextReviews` 里的 U+2066/U+2069（会渲染成豆腐块）。
5. **顺手补的一条门**（见上面 1 的最后一条）：没翻面时按 `1-4` 不再评分。

验证：功能探针 48/48（快捷键分层 6、入口不装钩子 2、窗口内卡片键 4、偷瞄正交 11、缩放 5、
页脚 10、翻面门 2、收尾 4）、书页回归 28/28（真书 113029 字、翻页、老板键、偷瞄滚轮、
安全文档来回、书页缩放、背诵入口与后台线程收尾）、快捷键恢复探针 22/22
（四个模式键回到全局注册、隐藏后能用老板键叫回来、成功注册的不再建 QShortcut、
模拟全局注册失败时 QShortcut 兜底、交互键仍然只在窗口内 / 偷瞄那层捕获）。
**SRS 零改动**：探针全程用记录器替换评分/翻面/撤销，日志里 `评分成功` 只有主人自己
19:43 那两条。
探针另加了三条卫生约束（后两轮才发现，值得留档）：① 给 `SetWindowsHookExW` 一个假句柄——
第一版探针真装了钩子，把主人此刻在别的窗口敲的键吞下来当成评分键；② 打断
`progress_save_timer.timeout`（它在 `__init__` 里绑死，后打桩拦不住，会写主人的 config.json）；
③ 探针窗口 `WA_ShowWithoutActivating`，不抢主人的键盘焦点。

### 批次 4.8（试用后的三条：列出牌组没用 / Anki 窗口看不见 / 切背诵强制窗口宽度）

1. **「列出牌组…」报"读不到牌组"**：`getDeckStats` **只要有一个牌组查不到计数就整批失败**
   （本机默认牌组「系统默认」单独查也报 "Deck counts unavailable."），而代码正是把 26 个牌组
   一次性批给它的。现在 `deck_stats()` 批量失败后退化成逐个查：查得到的收下、查不到的跳过，
   设置里写成 `（计数不可用）`，绝不因为一个坏牌组把整张列表打掉。实测 25/26 拿到计数。
2. **Anki 窗口"始终看不见"**：三个成因叠在一起，逐个修——
   - 藏之前 Anki 如果是**最小化**的，`SW_SHOWNOACTIVATE` 不会把它展开（`IsIconic` 还是真），
     于是"还原了但人还是看不见"。现在还原后查一次 `IsIconic`，还缩着就 `SW_RESTORE`。
   - 最小化窗口的 `GetWindowRect` 是 `@-16000,-16000` 这种垃圾值（这个坑代码注释里早记过），
     现在位置体检查 `GetWindowPlacement().rcNormalPosition`，并逐显示器判交集
     （双屏摆副屏是正常的），整个落在屏幕外才搬回主屏居中。
   - 进程被任务管理器/崩溃强杀时来不及还原：进背诵时落一个 `anki_concealed_by_us` 标记、
     还原成功清掉，**启动时体检一次**（有标记就显示，没标记只做位置体检，不碰人自己最小化的
     Anki）。托盘菜单另加一条「叫回 Anki 窗口」手动兜底。
3. **切背诵会强制窗口宽度**：页脚四个按钮的横向策略是 `Fixed`，把 top-level 的
   `minimumSizeHint` 从 69 顶到 494，于是进背诵的瞬间窗口 429 → 494。改成 `Ignored`
   （最小 0，空间够时仍按 `sizeHint` 显示），并在缩放时只重跑文字（`fill_review_bar`，
   不重套样式表）。量文字宽度必须按**页脚真实字号**建 `QFontMetrics`——用
   `review_bar.fontMetrics()` 拿到的是页脚控件的默认 12px，会永远判成"够宽"（踩过一次）。

### 批次 4.9（页脚的两条反馈：间隔不能丢 / 不要底色）

1. **"重来困难下一次就多的信息消失了，这个很重要"**：4.8 里为了不顶宽窗口，窄窗直接把按钮
   文字缩成 `1 重来`，把 Anki 给的间隔舍掉了——取舍做错了。现在改成**折行不砍字**：量出带
   间隔的文字宽度，算出每行放几个（429 宽下 2+2 两行，页脚高 78px），`1 重来 <1 分` 的
   间隔一个字都不省；只有"一行连一个带间隔的按钮都放不下"（窗口 <140px 级）才退到短文字。
   右侧提示同样不硬砍：从长到短挑第一句放得下的（`空格翻面 · Ctrl+D 收起` →
   `空格翻面 Ctrl+D` → `空格翻面`），且挤在最后一行末尾，不为它多占一行正文高度。
   折行的行数计算还会给提示预留位置（最多三分之一宽度），宁可多折一行也不让提示挤成半截字。
2. **"anki 下方这个区块不要用背景色，太显眼"**：去掉 4.7 加的同色衬底
   （`QWidget#reviewBar` 的 `background` 改回 `transparent`，去掉 `border-radius` 面板感），
   按钮描边从 alpha 85 降到 60、圆角 4 → 3，悬停从"填色"改成"只点亮描边"。

验证：页脚 17/17（429 折行 + 间隔全在 + 宽度 429 不变 + 提示整句 + 页脚高 78 ≤ 84 +
无底色 + 900 恢复一行四个 + 260 极窄才退短文字 + 收尾），全程不评分。

### 批次 4.10（页脚第三条反馈：不要操作提示 / 不要固定高度 / 不许压正文）

1. **"不用操作提示，只要必要的信息"**：整个提示标签删掉（`review_hint_label` 连同它那三档
   长短逻辑一起移除），页脚只剩学习计划（`新 20 · 学 4 · 复 160　今日已 1`）和四个评分按钮。
   顺带把边距收紧到 `(6,2,6,2)`。
2. **"不要固定的高度"**：页脚高度完全由内容决定，实测三种情形——宽窗一行四个 46px、
   429 宽的窄窗折成 3+1 两行 74px、没有计数时计划行 `setVisible(False)` 自己收起只剩 28px。
3. **"会覆盖上方的内容区域"**：先量清楚再动手——`text_edit` 到 y=712、页脚从 y=713 起，
   **本来是上下堆叠、没有重叠**；正文底部还有 36px 页边距留白。所以这条只能靠人眼确认到底
   看到的是什么（已向真人追问：是最后一行被挡、是正文区变矮、还是页脚本身画了底色）。

验证：页脚 14/14（无提示标签 + 不重叠 + 间隔全在 + 窗口宽度 429 不变 + 高度随内容变
74/46/28 + 计划行按有无计数收起与回来 + 退出收起 + 收尾），全程不评分。

### 批次 4.11（页脚的最终形态：一行字）

真人对 4.10 的复核给出了决定性的两句：**"不用按钮，有有效信息就行"** +
**"从新xx到正文内容有空白，会挡住有效内容"**。也就是说：① 页脚的四个 `QPushButton`
本来就是多余的（评分走 `1-4` 键和右键菜单），它们才是"操作"那部分；② 上一版折行后剩下的
半行空网格就是那段"空白"，白吃掉正文高度。

于是页脚改成**一个 `QLabel` 的纯文字**：
- 内容 = `新 20 · 学 4 · 复 160　今日已 1　1 重来 <1 分　2 困难 <6 分　3 良好 <10 分　4 容易 9 天`
  —— 还剩多少 + 每个评分键的后果（`nextReviews`），全部当**信息**显示。
- 折行自己算（不用 `QLabel` 的 wordWrap，否则会在 `1 重来` 与 `<1 分` 之间断开）：
  实测 429 宽 = **两行 40px**（正文可用高度因此从 713 涨到 747）、900 宽 = **一行 21px**、
  无计数时再短一行。没有空行、没有固定高度。
- 删掉 `review_buttons` / `review_btn_grid` / `review_plan_label` / `review_hint_label`
  以及 `_layout_review_buttons()`；`apply_review_bar_style()` 里的 `QPushButton` 规则一并删除；
  不再需要 `QGridLayout` 导入。评分路径不变（`review_grade_button` 仍由键盘、右键菜单、
  待评分定时器调用）。

验证：页脚 18/18（无按钮控件 + 只有一个文字标签 + 间隔全在 + 无提示字样 + 高度=行数×行高 +
与正文不重叠 + 宽度 429 不变 + 宽窗更矮更紧凑 + 数字键 4 仍能评分 + 无计数时内容换成纯评分
后果且不留空行 + 退出收起 + 收尾），全程不评分。

验证：本批三条 26/26（牌组容忍 4、宽度与自适应文字 8、Anki 窗口还原 12、收尾 2）、
书页精简回归 17/17（全局热键仍四个、真书 113029 字、翻页、老板键、偷瞄、安全文档、
背诵进出与宽度不变、窗口内空格翻面、后台线程收尾）。全程不评分。

### 实施中相对计划的修正（都以实测为准）

1. **吞键钩子不需要在重挂热键后补装**：`GlobalInputHook` 用的是自己那套 `ctypes` 低级钩子
   （`SetWindowsHookExW`），`keyboard.unhook_all()` 只拆 keyboard 库自己的钩子，够不着它；
   老板键看门狗每 90 秒的例行重挂因此不会影响背诵吞键。计划里"重挂后必须补装"这条不成立，
   只在 `recover_hotkeys` 里保留一次防御性补装。
2. **翻面之后空格不评分**：Anki 里空格=良好，误按一下就把这张卡的排程改了。空格/回车只负责
   翻面；评分一律用 1-4。同时补了**鼠标点卡片翻面**（和 Anki 一样），所以设置里关掉
   "连空格也吞"之后仍有完整的翻面路径。
3. **安全文档可以盖在背诵上**（原计划只写了"从安全文档按背诵键"这一半）：现在双向都通——
   背诵中按 `Ctrl+Alt+D` 换成安全文档（卡片先收着，`safe_snapshot` 只记一个 `from_review` 标记，
   **绝不拍书的快照**，否则拍下来的是卡片坐标），再按一次回到**同一张卡**；
   安全文档盖着的时候 `review_keys_active()` 为 False（屏幕上写着周报，敲不出空格立刻露馅），
   `render_card()` 也直接返回（否则 2.5 秒一次的对账会把卡片画到周报上）。
4. **偷瞄永远是"书"**：`Ctrl+E` 先退出背诵（书页按快照回来）、再退出安全文档，然后才浮现书页。

