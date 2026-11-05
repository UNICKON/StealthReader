自带字体（可选）
================

把字体文件（.ttf / .otf / .ttc / .otc）直接丢进这个目录，**重启程序**就生效，
不用装进系统：程序启动时会用 QFontDatabase.addApplicationFont 私有加载，
日志里能看到「自带字体已加载：<族名>」。把文件删掉重启就彻底没了。

现在放着的（2026-11-05 装）：MiSans 七个字重
    MiSans-Thin / ExtraLight / Light / Regular / Medium / Demibold / Bold
族名是 `MiSans`（其余几个也各自是一个族名，如 `MiSans Light`），
Qt 里 `MiSans` 这一个族带 Bold/Demibold/Light/Medium/Thin 全套样式，
所以 `font-weight:600` 这类富文本字重也真的会跟着变粗。

为什么需要它：
- 社区页「灵犀」方案的字体栈第一顺位就是 MiSans；
- 阅读页（书页正文、卡片、页脚）会先过 `resolve_font_family()`：
  真有 MiSans 就用 MiSans，没有就落到 微软雅黑，**不会**让 Qt 悄悄替成宋体。
  实测同一行 17px 字：请求 MiSans 而没装 → 暗像素 1244 / 墨量 251235（就是宋体那副样子），
  装了 MiSans → 2660 / 481950，雅黑 Regular → 2315 / 410454，宋体 → 1499 / 314790。

来源与许可：MiSans 由小米发布（https://hyperos.mi.com/font/），其许可协议允许免费使用，
但**不允许再分发字体文件本身**，所以 `fonts/*.ttf` 不进仓库（见 .gitignore），
只留在本机。程序里用到 MiSans 的地方已在 README 注明。
