# 桌面版与打包排错（历史文档）

> 主线说明与最新打包命令见仓库根目录 [README.md](../README.md)。本文件保留 WebView2 / 中文路径 / 单文件解压等方面的排错细节；其中提到的旧目录结构（templates/、tools/、IconSwap.spec）已被 src/ + bin/ 取代。

把现有的 Flask 界面装进一个独立的桌面窗口（pywebview），可用 PyInstaller 打包成
单个 exe，双击即开，不需要浏览器、不需要命令行。

改造是**增量**的：`app.py` 的 `create_app()` 与全部路由保持原样，只把 `__main__`
块换成调用桌面启动器；新增 `src/ui_window.py` 负责"后台 Flask 线程 + 前台窗口"。

---

## 1. 环境要求

- Windows 10 / 11
- Python 3.10+
- 运行 exe 的机器需要 **Microsoft Edge WebView2 Runtime**（Win11 自带；
  Win10 一般随 Edge 安装，缺失时窗口会打不开或白屏，可从微软官网下载安装）

## 2. 安装依赖

```bat
cd /d "E:\deepseek harness指定工作区\项目企划书\IconSwap应用程序"
python -m pip install flask pillow
python -m pip install -r requirements-desktop.txt
```

`requirements-desktop.txt` 里是 `pywebview` 和 `pyinstaller`（浏览器版不需要它们）。

## 3. 开发模式运行（弹出桌面窗口）

```bat
python src/ui_window.py
```

- 窗口标题 `IconSwap`，初始 900×720，可缩放，最小 720×560
- Flask 只监听 `127.0.0.1`，端口由系统自动分配**空闲端口**（不写死 5000，
  不会和别的程序抢端口）
- 关掉窗口 → Flask 线程自动停止，进程退出
- 想临时看日志/调试前端：`set ICONS_DEBUG_GUI=1` 后再运行

`python app.py` 现在**同样**打开桌面窗口（`__main__` 已改为调用桌面启动器）。
只想用浏览器调试时可运行：

```bat
flask --app app run --port 5000
```

## 4. 打包成 exe

双击 `build_exe.bat`（或在根目录执行）：

```bat
build_exe.bat
```

脚本会先检查 PyInstaller / pywebview 是否就绪，然后**自动挑一个可写的纯英文临时目录**
（`%PUBLIC%\IconSwapTemp` → `C:\IconSwapTemp` → `%LOCALAPPDATA%\IconSwapTemp`，
都不行就退回默认 `%TEMP%` 并给出警告），再执行（**完整命令**）：

```bat
REM 1) 选一个纯英文、普通用户可写的运行时临时目录
REM    优先 Public：父目录一定存在且普通用户可写，路径可安全地"烧"进 exe
set "ISW_TMPDIR="
call :try_tmpdir "%PUBLIC%\IconSwapTemp"
if not defined ISW_TMPDIR call :try_tmpdir "C:\IconSwapTemp"
if not defined ISW_TMPDIR call :try_tmpdir "%LOCALAPPDATA%\IconSwapTemp"

REM 2) 打包
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name IconSwap ^
  --distpath "." ^
  --icon "assets\iconswap.ico" ^
  --runtime-tmpdir "%ISW_TMPDIR%" ^
  --add-data "src/iconswap/templates;iconswap/templates" ^
  --add-data "src/iconswap/static;iconswap/static" ^
  --add-data "assets;assets" ^
  --add-data "bin;bin" ^
  --hidden-import webview ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import clr ^
  --hidden-import pefile ^
  --hidden-import win32com ^
  --hidden-import win32com.client ^
  --hidden-import pythoncom ^
  --hidden-import pywintypes ^
  --hidden-import win32api ^
  --collect-all pythonnet ^
  --exclude-module numpy ^
  --exclude-module scipy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module tkinter ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module PySide2 ^
  --exclude-module PySide6 ^
  --exclude-module qtpy ^
  --exclude-module IPython ^
  --exclude-module black ^
  --exclude-module yapf ^
  --exclude-module blib2to3 ^
  --exclude-module zmq ^
  --exclude-module pytest ^
  src/ui_window.py
```

> `--runtime-tmpdir` 只接受**字面路径**（不支持 `%VAR%` 展开），所以脚本先把
> 环境变量展开成绝对路径再传给 PyInstaller；想固定成 `C:\IconSwapTemp`，
> 用管理员先 `mkdir C:\IconSwapTemp` 即可（脚本会优先选它）。

- `--onefile`：单个 exe；`--windowed`：不弹黑色控制台窗口
- `--runtime-tmpdir`：**固定 onefile 的解压目录**，解决
  `Could not create temporary directory!`。详见下面的「运行时临时目录」
- `--add-data`：把 `templates\`、`static\`、`tools\` 打进包内，运行时解压到
  `_MEIPASS`，`src/ui_window.py._point_app_at_resources()` 会把 Flask 的
  模板/静态目录指过去
- `--hidden-import` + `--collect-all pythonnet`：保证 pywebview 的 Windows 后端
  （EdgeChromium / WinForms）与 pythonnet 被正确收集
- `--exclude-module`：排除 numpy / scipy / pandas / matplotlib / tkinter / Qt 等
  本应用不需要的包。**不排除时 exe 约 220 MB**（其中 Intel MKL 的
  `mkl_*.dll` 就占约 400 MB 未压缩），排除后约 **28 MB**，启动约 2–3 秒

产物位置：**项目根目录的 `IconSwap.exe`**（`--distpath "."`）。中间文件在 `build\`，可随时删除。

也可以直接用 spec 构建（参数已固化在 `IconSwap.spec` 里）：

```bat
python -m PyInstaller --clean --distpath "." IconSwap.spec
```

> 注意保留 `--distpath "."`：`IconSwap.spec` 不记录输出目录，缺省会落到 `dist\`，
> 与"exe 放在项目根"的约定不一致。

### 运行时临时目录（`Could not create temporary directory!` 的根因与修法）

onefile 的引导程序会先把整个包解压到临时目录再运行 Python。如果那个目录
不可用（被安全软件拦、路径含非 ASCII、权限不足），就会报
`Could not create temporary directory!` 并直接退出。

`--runtime-tmpdir` 把它固定到一个纯英文路径，本项目用
**`C:\Users\Public\IconSwapTemp`**。选这个路径而不是 `C:\IconSwapTemp` 的原因：

| 候选 | 纯英文 | 普通用户可创建 | 结论 |
|---|---|---|---|
| `C:\IconSwapTemp` | ✅ | ❌ 需要管理员（C 盘根目录） | 首次运行仍会报同样的错 |
| `C:\Users\Public\IconSwapTemp` | ✅ | ✅ | **采用** |
| `%TEMP%`（默认） | 取决于用户名 | ✅ | 用户名是中文时会出问题 |

两点补充：

1. **引导程序会自己创建这个目录**（实测 PyInstaller 6.21：目录不存在也能正常
   启动，运行后目录保留、只删除 `_MEIxxxx` 子目录）。所以不需要预建。
2. `src/ui_window.py.ensure_runtime_tmpdir()` 仍会在启动时检查并补建一次，
   用于"用户手动删除了该目录"之后的下一次启动；它**救不了当场那一次**——
   Python 代码是在解压成功之后才执行的。
3. 想坚持用 `C:\IconSwapTemp`：先用管理员执行一次
   `mkdir C:\IconSwapTemp`，再把它传给 `--runtime-tmpdir` 即可。
4. 环境变量 `ICONSWAP_TMPDIR` 可覆盖代码侧认定的路径（仅影响启动自检，不影响
   已编译进 exe 的解压目录）。

## 5. 使用

双击 `IconSwap.exe` → 直接弹出 `IconSwap` 桌面窗口 → 拖图、选尺寸、生成、下载，
与浏览器访问完全一致。

首次启动会先弹一个**风险确认对话框**（见第 5.1 节）。

下载的 `.ico` 会进系统的"下载"目录（与浏览器行为相同）。

### 5.1 首次运行的风险确认

程序会修改 exe 文件的图标，存在被安全软件拦截或文件损坏的风险，因此第一次
启动时会弹出系统对话框：

> 本工具会修改 exe 文件图标，存在被安全软件拦截或文件损坏的风险。
> 请确保已备份重要数据。是否继续使用？

- **确定 / 是** → 写入配置并进入主界面，之后启动不再询问
- **取消 / 否**（也是默认按钮，防止误触）→ 程序直接退出

配置文件：**`%USERPROFILE%\.iconswap_config.json`**

```json
{
  "risk_accepted": true,
  "version": 1
}
```

- 删除该文件即可让下次启动重新询问
- 判定条件是 `risk_accepted` **严格等于布尔 `true`**；文件缺失、损坏、
  写成字符串 `"true"` 都会重新询问
- 自动化/批量场景可设 `set ICONSWAP_SKIP_RISK_PROMPT=1` 跳过弹窗（不写配置）
- 实现细节：GUI 框架 pywebview 没有 messagebox，因此用 Windows 原生
  `user32.MessageBoxW`（与窗口同一套系统 UI），并在 `webview.start()` 之前、
  主线程上调用，返回后才会创建窗口

> ⚠️ **关于中文路径（已自动处理，无需手动拷贝）**
>
> 实测原因：pywebview 依赖的 pythonnet 在冻结环境下加载 `Python.Runtime.dll`，
> 当 **exe 所在目录**或**临时解压目录**含非 ASCII 字符时，.NET 侧解析失败并报
> `RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize`，窗口无法创建。
> 本项目的目录 `...\项目企划书\IconSwap应用程序\` 正好命中该问题。
>
> `src/ui_window.py` 已内置**自搬运启动**：双击 exe 时若检测到路径含非 ASCII
> 字符，会先把 exe 复制到纯英文暂存目录，再从那里重启自己（并带上环境标记，不会
> 无限重启）。默认暂存位置依次尝试：
>
> 1. `%LOCALAPPDATA%\IconSwap\runtime`（通常是 `C:\Users\<用户名>\AppData\Local\...`）
> 2. `%PUBLIC%\IconSwap\runtime`
> 3. `%PROGRAMDATA%\IconSwap\runtime`
>
> 可用环境变量 `ICONSWAP_STAGE_DIR` 指定首选暂存目录（需为纯英文路径），例如：
>
> ```bat
> set ICONSWAP_STAGE_DIR=D:\IconSwapRuntime
> IconSwap.exe
> ```
>
> 首次启动会复制一次（约 24 MB，通常 1 秒内），之后命中大小/时间相同的判断，
> 不再重复复制。运行日志不写在程序目录，而是落在临时目录（`%TEMP%\IconSwap\IconSwap.log`，或 `C:\IconSwapTemp\IconSwap\IconSwap.log`）。
>
> 注意：暂存目录必须是**物理上**位于纯英文路径的普通目录；用目录联接（junction）
> 指回中文目录无效——.NET 会解析出真实路径。

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `缺少 pywebview` | 执行 `python -m pip install -r requirements-desktop.txt` |
| `Could not create temporary directory!` | 临时解压目录不可写（权限/杀软），换 TEMP 或换目录 |
| `Failed to resolve Python.Runtime.Loader.Initialize` | **exe 或 TEMP 路径含中文**；正常双击已自动搬运。若仍出现，说明暂存目录也不可用——用 `ICONSWAP_STAGE_DIR` 指到一个纯英文可写目录 |
| 双击后瞬间消失、日志里只有"所有暂存目录都不可用" | 三个默认暂存位置都无写权限（受控机器/只读环境），设 `ICONSWAP_STAGE_DIR` |
| 窗口白屏 / 起不来 | 装 WebView2 Runtime；或设 `ICONS_DEBUG_GUI=1` 看日志 |
| 打包后双击没反应 | 看 `%TEMP%\IconSwap\IconSwap.log` 或 `C:\IconSwapTemp\IconSwap\IconSwap.log`；多为缺 hidden-import |
| 杀毒软件拦截 | 单文件 exe 首次启动会解压，部分杀软误报，加信任即可 |
| 端口冲突 | 不存在：每次启动都向系统申请空闲端口 |
| 想临时换回浏览器 | `flask --app app run --port 5000`，不影响桌面版 |

## 7. 文件职责

| 文件 | 作用 |
|---|---|
| `src/ui_window.py` | 后台 Flask 线程（`FlaskServer`）+ pywebview 窗口生命周期 |
| `app.py` | Flask 应用工厂与路由（未改动功能），`__main__` 调桌面启动器 |
| `build_exe.bat` | PyInstaller 打包脚本 |
| `requirements-desktop.txt` | 桌面版额外依赖 |
| `src/iconswap/templates/` / `src/iconswap/static/` | 前端资源，打包时随包带上 |
| `bin\rcedit.exe` | 资源编辑工具，打包时随包带上（批量替换图标用） |
| `THIRD-PARTY-NOTICES.md` | 第三方组件与许可证声明（界面「关于」里也能看到） |

## 8. 第三方组件

程序内点击页脚「关于 · 版本与第三方组件」可查看实际版本与许可证；
对应的静态清单在 `THIRD-PARTY-NOTICES.md`。

再分发 `IconSwap.exe` 时请保留 rcedit（MIT，© Electron）的版权与许可声明。

