# foliate-js（原样拷入，未改一行）

- 上游：https://github.com/johnfactotum/foliate-js
- commit：78914aef4466eb960965702401634c2cb348e9b1（2026-05-01）
- 许可证：MIT（见同目录 LICENSE）；vendor/zip.js 是 BSD-3-Clause，vendor/fflate.js 是 MIT
- 只拷了用得到的模块：view/epub/epubcfi/paginator/fixed-layout/overlayer/progress/text-walker/comic-book/mobi/fb2/search
- 升级方式：把上游同一条路径的这些文件整体覆盖，然后跑一遍 	ests/probe_web.py 的静态检查
