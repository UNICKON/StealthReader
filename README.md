# StealthReader

![version](https://img.shields.io/badge/version-1.0.0-blue)
![platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-lightgrey)

StealthReader 是一个 Windows 桌面阅读器。窗口呈无边框、置顶、半透明的浮层，不占任务栏，正文区域是一个只读文本视图。同一窗口可以承载三类内容：本地书籍、社区帖子与 Anki 卡片，它们共用同一套渲染与显示控制。

## 功能

### 阅读

- 支持 TXT 与 EPUB。EPUB 的目录同时解析 OPF manifest 与 NCX/nav 两种来源；TXT 自动识别 UTF-8 与 GBK 编码，并按标题行划分章节。
- 分页依据实际渲染结果计算：每次载入一段文字，下一页的起点由当前视口内的实测位置决定；改变字号、行距或窗口尺寸后，阅读位置不会错乱。
- 阅读进度以全书字符偏移保存，重新打开或调整排版后仍回到原处。
- 可设置字体、字号、行距、段间距、页边距与首行缩进。
- 选中文字可添加五色高亮与笔记；可为当前页添加书签；目录、书签、划线集中在一个面板中查看与跳转，并可导出为 Markdown。
- 可连接 Legado 的 Web 服务读取书架与正文，也可浏览微信读书的书架与阅读进度。

### 社区

各站点被统一为帖子、评论与内容块三种结构，因此不同来源的浏览方式和排版保持一致。

支持的来源包括 IT之家、知乎、微博、NGA、小红书、虎扑、linux.do、V2EX、GitHub 趋势与 Medium，以及自定义 RSS/Atom 订阅。

- 列表、正文与评论树在同一窗口内呈现；评论可逐条折叠，层级以缩进表示。
- 知乎按问题、回答、回答的评论三层组织，其余来源为帖子与评论。
- 内容为只读，不提供发帖、回复、点赞等写操作。
- 部分来源需要登录凭据才能读取正文与评论，可在设置中填写，或从本机浏览器导入。

### Anki 卡片

- 通过 AnkiConnect 与本地 Anki 通信，卡片以文档形式显示，使用键盘评分。
- 可将 Anki 自身窗口隐藏，退出该模式时恢复。
- 卡片内容不写入日志，也不保存到磁盘。

### 显示控制

| 功能 | 说明 |
| --- | --- |
| 老板键 | 隐藏或显示窗口，阅读状态完整保留，热键可自定义 |
| 偷瞄 | 不抢占焦点地显示窗口，并将鼠标滚轮用于翻页；滚动事件不会传递给下层窗口 |
| 自动配色 | 取窗口旁侧的屏幕颜色作为背景，并按亮度选择文字颜色 |
| 幽灵模式 | 鼠标离开窗口后文字透明，移入即恢复 |
| 防截屏 | 调用 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`，窗口不出现在截图与录屏中，需 Windows 10 2004 及以上 |
| 安全文档 | 将内容切换为内置工作文档或指定文件，退出后恢复原内容 |

## 快捷键

全局热键可在设置中修改。

| 键 | 作用 |
| --- | --- |
| 老板键（默认 `Esc`） | 隐藏或显示窗口 |
| `Ctrl+E` | 偷瞄 |
| `Ctrl+Alt+D` | 安全文档 |
| `Ctrl+Alt+F` | 社区 |
| `Ctrl+Alt+A` | Anki 卡片 |

阅读页：

| 键 | 作用 |
| --- | --- |
| `→` `↓` `Space` `PageDown` | 下一屏 |
| `←` `↑` `PageUp` | 上一屏 |
| `Ctrl+B` | 为当前页添加书签 |
| `Ctrl+L` | 目录、书签与划线 |

社区页：

| 键 | 作用 |
| --- | --- |
| `J` / `K` | 选择上一条或下一条 |
| `Enter` | 打开 |
| `Backspace` `Esc` | 返回 |
| `C` / `V` | 切换来源 / 切换板块 |
| `[` `]` | 前后切换来源 |
| `,` `.` | 前后切换板块 |
| `E` | 跳到评论区顶部 |
| `Z` | 返回主列表，或撤回上一次切换 |
| `R` / `O` | 重新获取 / 在浏览器中打开原链接 |
| `Space` / `PageUp` | 翻一屏 |

卡片页：`Space` 或 `Enter` 翻面，`1`–`4` 评分，`U` 撤销，`R` 重播音频，`Esc` 退出。

## 安装

### 预编译版本

从 Releases 下载 `StealthReader-1.0.0-win64.zip`，解压到任意目录后运行 `StealthReader.exe`。不需要安装 Python 或额外依赖。

程序将配置、缓存与日志写入自身所在目录，整个文件夹可直接复制或删除。

若全局热键不生效，请以管理员身份运行：部分系统策略会限制普通权限进程注册全局键盘与鼠标钩子。

### 从源码运行

要求 Windows 10/11 与 Python 3.12。

```
pip install "PyQt5==5.15.*" keyboard requests
python main.py
```

`cryptography` 或 `pycryptodome` 为可选依赖，仅用于解密 Chromium 系浏览器的 Cookie。

## 使用

首次运行会在程序目录生成 `config.json`，各项设置保存在其中，也可以在设置界面修改。

- **书籍**：从菜单打开本地 TXT 或 EPUB 文件。使用 Legado 时，在设置中填写其 Web 服务地址。
- **社区**：按 `Ctrl+Alt+F` 进入。需要登录的来源在设置中填写 Cookie，或使用 Cookie 导入功能。
- **卡片**：需要本地 Anki 正在运行，并安装 [AnkiConnect](https://ankiweb.net/shared/info/2055492159) 插件。安装后按 `Ctrl+Alt+A` 进入，可在设置中选择牌组。

## 配置

`config.json` 中的常用项：

| 键 | 说明 |
| --- | --- |
| `ip` | Legado Web 服务地址 |
| `opacity` `font_family` `font_size` | 窗口透明度与正文字体 |
| `line_spacing` `paragraph_spacing` `page_margin_chars` `first_line_indent` | 排版参数 |
| `page_overlap_lines` | 翻页时保留的上一页行数，默认 2；设为 0 则新一页从上一页的下一行开始 |
| `boss_key` `peek_key` `safe_key` `anki_key` `feed_key` | 五个全局热键 |
| `safe_doc_path` | 安全文档使用的文件，留空则使用内置文档 |
| `anki_deck` `anki_conceal` `anki_api_key` | 默认牌组、是否隐藏 Anki 窗口、AnkiConnect 密钥 |
| `feed_sources` `feed_rss` | 启用哪些社区来源，以及订阅地址 |
| `ghost_mode` `auto_mode` `antishot_mode` | 幽灵模式、自动配色、防截屏 |

站点凭据保存在程序目录的 `feed_auth.json`：

```json
{
  "zhihu": { "cookie": "...", "user_agent": "..." }
}
```

## 数据与隐私

程序不收集使用数据，也不向作者或第三方发送统计信息。访问网络仅发生在以下情况：打开社区页、使用联网书源、浏览微信读书书架、启用 Cookie 导入。

程序目录中会生成以下文件：

| 文件 | 内容 |
| --- | --- |
| `config.json` | 设置 |
| `annotations.json` | 高亮、笔记与书签 |
| `feed_auth.json` | 各站点登录凭据 |
| `cache/` | 社区内容与图片缓存 |
| `stealthreader.log` | 运行日志，不含卡片内容 |

`feed_auth.json` 为明文 JSON，其中的 Cookie 等同于对应站点的登录状态。该文件已在 `.gitignore` 中排除，请勿分享或提交。

## 已知限制

- 仅支持 Windows。
- 社区部分为只读，且依赖各站点公开接口，接口变动或风控收紧时可能需要更新 Cookie 或等待适配。
- 微信读书仅支持书架与阅读进度，不包含正文、划线导出。
- 提供的是压缩包而非安装程序，也不包含自动更新。

## 项目结构

| 文件 | 职责 |
| --- | --- |
| `main.py` | 窗口、渲染、分页、显示控制与各模式的状态管理 |
| `feed.py` | 社区数据的通用模型、排版与导航 |
| `feed_net.py` | 网络请求、限速、超时与缓存 |
| `feed_sources.py` | 各站点适配器与混流 |
| `feed_more.py` | 订阅类来源（RSS、Medium、GitHub 趋势） |
| `feed_cookies.py` | 从本机浏览器读取 Cookie |
| `xhs_sign.py` | 小红书请求签名 |
| `anki_client.py` | AnkiConnect 客户端 |
| `weread.py` | 微信读书书架与进度 |
| `hupu_boards.py` | 虎扑板块表 |

## 测试

`tests/` 下的探针脚本在离线状态下运行：网络层被替换为固定响应，适配器只读取 `tests/fixtures/` 中裁剪过的真实响应样本，因此不需要网络与登录状态。`probe_live.py` 会实际发起请求，仅在需要时手动运行。

## 许可

本仓库未声明许可证。
