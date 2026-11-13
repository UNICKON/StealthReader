# 怎么打一个发行包（Windows）

从 v1.0.0 起发行物是**一个 zip**：里面 `StealthReader.exe` + `_internal/`，解压即用，不要求装 Python。
下面是 1.1.0 实际用的步骤（照抄就能复现）。

## 1. 装 PyInstaller（一次性）

```powershell
$py = "C:\Users\KSO\AppData\Local\Programs\DSH Desktop\resources\office-runtime\primary-runtime\dependencies\python\python.exe"
& $py -m pip install --disable-pip-version-check pyinstaller
```

这台机器上唯一带 `PyQt5` 的 Python 就是 DSH 的 office-runtime 那份（3.12.14），但 **`PyQt5` 不在
它的 site-packages 里**——源码运行时是启动器把仓库里的 `libs/` 塞进 `PYTHONPATH` 的
（见 `run_stealthreader.cmd`）。所以打包时必须显式告诉 PyInstaller 去 `libs/` 找：

## 2. 打包

```powershell
Set-Location <仓库根>
$out = "$env:TEMP\sr_build"
& $py -m PyInstaller --noconfirm --clean --windowed --onedir `
  --name StealthReader `
  --icon "$PWD\read.ico" `
  --paths "$PWD\libs" `
  --add-data "$PWD\feed_demo;feed_demo" `
  --hidden-import keyboard._winkeyboard `
  --distpath "$out\dist" --workpath "$out\build" --specpath $out `
  "$PWD\main.py"
```

三个坑，都是真踩过的：

- **`--paths libs` 不能省**。省了 PyInstaller 会报 `Hidden import "sip" not found` 之类，打出来的
  exe 启动就 `ImportError: No module named 'PyQt5'`。
- **`--add-data` 的源路径要写绝对路径**。它是相对 `--specpath`（这里是 `$env:TEMP\sr_build`）
  解析的，写 `feed_demo;feed_demo` 会报 `Unable to find ...\sr_build\feed_demo`。
- **`feed_demo` 必须进包**。`main.py` 里 `RES_DIR` 在冻结后指向 `_MEIPASS`，社区页的示例数据从
  那里读；只把 `feed_demo/` 摆在 exe 旁边是没用的。
- `fonts/` **不要打进包**（仓库里 8 个 CJK 字体 54 MB）。`main.py` 是从 `APP_DIR/fonts`
  （exe 旁边）加载的，用户自己往里丢就行。

产物：`dist\StealthReader\`，187 个文件、97 MB。

## 3. 冒烟测（必做）

冻结版也有单实例互斥体（命名互斥体，全局的），所以**先把正在跑的那个关掉**再测，否则只能看到
「单实例：已经有一个在跑，已叫它把窗口亮出来」。

```powershell
$dst = "$env:TEMP\sr_release_smoke"
Copy-Item -Recurse "$env:TEMP\sr_build\dist\StealthReader" $dst
Start-Process "$dst\StealthReader.exe" -ArgumentList "--hidden" -WorkingDirectory $dst
Start-Sleep 16
Get-Content "$dst\stealthreader.log" -Encoding UTF8 -Tail 12
Stop-Process -Id (Get-Process StealthReader).Id -Force
```

日志里应该出现「应用启动」和「解释器 3.12.14（…\StealthReader.exe）」——**后一行必须是 exe 自己**，
不是某个 python.exe。热键、会话通知、字体解析那几行也都该在。测完把源码版重新起起来。

## 4. 压缩、打 tag、发 release

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
  "$env:TEMP\sr_build\dist\StealthReader",
  "$env:TEMP\sr_build\StealthReader-1.1.0-win64.zip",
  [System.IO.Compression.CompressionLevel]::Optimal, $false)   # $false：不要在 zip 里多套一层目录

git tag -a v1.1.0 -m "StealthReader 1.1.0"
git push myfork master; git push myfork v1.1.0
```

release 用 API 建（这台机器上没有 `gh`；token 从 git 凭据里取，`git credential fill`）：

- `POST /repos/UNICKON/StealthReader/releases`（`tag_name` / `name` / `body` / `draft:false`）
- `POST https://uploads.github.com/repos/.../releases/<id>/assets?name=<文件名>`，body 直接是 zip 字节

发完**把资产下回来对一遍 SHA256**（`Get-FileHash`），别只看 HTTP 200。

## 已知缺口

打包版里 **没有 AES 库**（`cryptography`、`pycryptodome` 都没装进包），`feed_cookies` 的
「从浏览器导入 Cookie」会报「这台机器上没有 AES 库」。要补就在打包前
`pip install cryptography`，它会跟着进包（体积 +几 MB）。
