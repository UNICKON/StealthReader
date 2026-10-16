# 探针（社区统一流的验收）

这一批功能的验收不是"我觉得能跑"，而是三个能重跑的探针。改动社区那一层之后，
至少把前两个跑一遍——它们是零网络的，几秒钟出结果，不碰网络也不碰书架。

```powershell
$py = "$env:APPDATA\WPS 灵犀\python-env\python.exe"     # 就是跑 main.py 的那个解释器

& $py -X utf8 tests\probe_sources.py     # 来源层逻辑（零网络）
& $py -X utf8 tests\probe_ui.py          # 界面集成（零网络，离屏 Qt 走完整窗）
& $py -X utf8 tests\probe_live.py        # 真机联网冒烟（会真的发请求，跑完自清缓存）
```

## 三个探针各管什么

| 探针 | 网络 | 管什么 |
|---|---|---|
| `probe_sources.py` | **零** | 每个来源的 列表 / 正文 / 评论 映射、统计单位、缓存与断网回落、混流分额与转发、结构不变量 |
| `probe_ui.py` | **零** | 真适配器 + 替身 Http 走完整窗：混流席位、`[` `]` 切来源、`,` `.` 切栏目、详情、`O` 开原帖、退出不动书的状态、两个"结果晚到"的竞态 |
| `probe_live.py` | 真 | 真站能不能拿到、缓存第二次是否 0 秒、混流是否并行、整窗真拉一遍 |

**"零网络"是可以验证的，不是靠自觉**：`probe_sources.py` / `probe_ui.py` 开头把
`requests.get / post / Session.get` 全换成抛异常，而所有适配器拿到的是一个读夹具的替身 Http。
所以只要探针是绿的，就说明这次一个网络包都没发。

`probe_ui.py` 离屏跑（`QT_QPA_PLATFORM=offscreen`），会短暂开一个窗口对象然后销毁；
它把 `save_annotations`、`log_event`、全局热键、`os.startfile` 全换成桩，
并且停掉进度保存定时器——**不会写 config.json，不会动阅读进度，不会真的开浏览器**。

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

最后一行的来源要看清楚：**不是 linux.do**。linux.do 前面挂着 Cloudflare 盾，本机录不到
（403/超时），而 Discourse 适配器（回复树、最后回复时间、广告帖兜底）没有夹具就等于没人盯，
所以退回官方的 meta.discourse.org 录——两边是同一种 Discourse，结构一致。
linux.do 特有的那部分（`cf_clearance` + UA 必须一致）不在这个夹具的覆盖范围里，
真机能不能过只能靠 `probe_live.py` 和实际使用时的页脚反馈。

录不到的站**不写空文件**：那个站对应的探针章节自己会 `skip`（并在结果里报"跳过 N 项"），
所以夹具目录里"缺哪个文件"本身就是一条信息。
