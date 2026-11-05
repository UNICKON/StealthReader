自带字体（可选）
================

把字体文件（.ttf / .otf / .ttc / .otc）直接丢进这个目录，**重启程序**就生效，
不用装进系统：程序启动时会用 QFontDatabase.addApplicationFont 私有加载，
日志里能看到「自带字体已加载：<族名>」。把文件删掉重启就彻底没了。

为什么要它：社区页的「灵犀」排版方案字体栈是
    MiSans → MiSans VF → Microsoft YaHei → 微软雅黑 → PingFang SC → Segoe UI
本机没装 MiSans 时，Qt 只能落到雅黑；`QFont("MiSans")` 这种写法更糟——本机没这个族时
Qt 会悄悄替成**宋体**（实测同一行字：MiSans → 墨点 1244，雅黑 → 2260）。所以要走
「字体栈先命中真装了的族名」这条，文件放这里就够了。

MiSans 是小米放出来的免费可商用字体，官网 https://hyperos.mi.com/font/download ，
下载后会有 MiSans-Regular.ttf / MiSans-Medium.ttf 等几个文件，丢进来即可。
（.ttf 二进制不进 git：见 .gitignore）
