# IconSwap

> Windows 图标转换与替换工具：图片转多尺寸 `.ico`，选一次图标，点一下 **🚀 一键智能替换**，EXE 与快捷方式一起换好。

IconSwap 是一个本地运行的 Windows 小工具（Flask + pywebview 桌面窗口，可打包成单文件 exe）：

- **图片 → ICO**：PNG / JPG / BMP / WebP / GIF / TIFF 一键转 16–256 多尺寸 `.ico`，支持去白边转透明；
- **一键智能替换**：全局图标源只选一次；目标可以是文件夹（可含子目录）或多选的 EXE/LNK 文件，合并进同一个列表，点一次按钮即可：
  - `.exe` → 用 `rcedit` 直接替换内嵌图标；改不了（数字签名 / 自解压单文件包 / 正在运行）就**自动降级**为在桌面生成 `原名 - 快捷方式.lnk` 并替换其图标，**原 exe 不动**；
  - `.lnk` → **不看它指向哪里**，直接把 `IconLocation` 覆盖为 `<图标绝对路径>,0` 并回读校验，**绝不新建任何快捷方式**；
  - 其它文件 / 异常 → 记一行 `[跳过]` 或 `[失败]`，绝不中断整批任务。
- **统一日志**：底部一块日志区，按 `[成功]` / `[降级]` / `[跳过]` / `[失败]` 前缀输出每个文件的处理结果。

程序不写图标库、不缓存图标：转换结果只以字节流返回给浏览器下载；替换时使用磁盘上真实存在的 `.ico`，因此快捷方式图标不会在程序退出后失效。

---

## 功能特性

| 功能 | 说明 |
|---|---|
| 多尺寸 ICO | Pillow 自绘 ICO 容器：≤48px 用 DIB/BMP 帧、256px 用 PNG 帧，资源管理器各尺寸都能正确渲染 |
| 去白边 | 从四边做连通域泛洪，只把与边缘连通的背景色变透明，人物衣服里的白色会保留 |
| 单一入口 | 图标源 / 目标选择 / 执行 / 日志都合并到一个面板，不再分两个模块 |
| 混合目标 | 文件夹扫描结果与手动多选文件合并成一份清单，后台按后缀自动分流 |
| EXE 图标替换 | `rcedit --set-icon`，替换前备份 `原名_original.exe`，替换后比对 PE 资源签名确认真的变了 |
| 自动降级 | 签名 exe / PyInstaller 单文件包 / 正在运行 / rcedit 报错 → 桌面快捷方式，不破坏原文件 |
| 快捷方式替换 | 直接改写 `IconLocation`，先试 `WScript.Shell`，失败自动改用 `IShellLinkW`，写后**必回读校验** |
| 无目标也照改 | `TargetPath` 为空的商店 / UWP 快捷方式、目标已删除的快捷方式，一律照改图标 |
| 强校验 | 失败绝不静默：异常原因写进 UI 日志与任务摘要（如"读回 xxx 与期望不符"） |
| 后台线程 | 替换任务在后台线程执行，前端轮询日志，界面不卡死 |

## 目录结构

```
IconSwap/
├── bin/                        # 外部工具（随包分发）
│   ├── rcedit.exe              # 改写 PE 资源（MIT，© Electron）
│   └── README.txt
├── assets/                     # 应用图标（打包用）
├── src/
│   ├── main.py                 # 启动入口：python src/main.py
│   ├── ui_window.py            # pywebview 桌面窗口 + 后台 Flask 服务 + 原生对话框
│   └── iconswap/               # 应用包
│       ├── __init__.py
│       ├── app.py              # Flask 工厂与 /api/convert（图片 → ICO）
│       ├── batch_api.py        # 批量接口：scan / start / jobs / tools
│       ├── batch_jobs.py       # 后台任务注册表（线程 + COM 初始化 + 日志）
│       ├── icon_replacer.py    # 替换编排：扫描 → 分流 → 替换/降级 → 汇总
│       ├── resource_tools.py   # rcedit 调用、备份、锁定检测、失败回滚、强校验
│       ├── shortcut_tools.py   # .lnk 读写（WScript.Shell / IShellLinkW / 写后校验）
│       ├── pe_inspector.py     # PE 解析、数字签名检测、exe 枚举
│       ├── ico_converter.py    # 多尺寸 ICO 编码
│       ├── image_tools.py      # 去白边（连通域）
│       ├── shell_cache.py      # 刷新资源管理器图标缓存
│       ├── shell_paths.py      # 桌面目录（兼容 OneDrive 重定向）
│       ├── windows_privileges.py
│       ├── risk_consent.py     # 首次运行风险确认
│       ├── about.py            # 版本与第三方组件
│       ├── paths.py            # bin/ 与包内资源定位（源码 / 打包通用）
│       ├── templates/index.html
│       └── static/{css,js}
├── tests/                      # pytest 用例（190+）
├── build_exe.bat              # 一键打包
├── requirements.txt           # 运行依赖
├── requirements-desktop.txt   # 桌面窗口 + 打包依赖
├── THIRD-PARTY-NOTICES.md
├── LICENSE
└── README.md
```

## 环境要求

- Windows 10 / 11（本项目只支持 Windows）
- Python 3.10+
- 运行打包后的 exe 需要 **Microsoft Edge WebView2 Runtime**（Win11 自带；Win10 一般随 Edge 安装）

## 安装

```bat
git clone <this-repo> IconSwap
cd IconSwap

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements-desktop.txt
```

只想用浏览器调试界面：

```bat
pip install -r requirements.txt
```

## 运行

桌面窗口：

```bat
python src\main.py
```

浏览器调试（不弹桌面窗口）：

```bat
flask --app src.iconswap.app run --port 5000
```

打开 <http://127.0.0.1:5000/>。

首次启动会弹一次风险确认（修改 exe 有风险，确认后写入 `%USERPROFILE%\.iconswap_config.json`）。
自动化场景可设 `ICONSWAP_SKIP_RISK_PROMPT=1` 跳过。

## 打包成单文件 exe

双击 `build_exe.bat`，或手动执行完整命令（在仓库根目录）：

```bat
set "ISW_TMPDIR=%PUBLIC%\IconSwapTemp"
if not exist "%ISW_TMPDIR%" mkdir "%ISW_TMPDIR%"

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name IconSwap ^
  --distpath "." ^
  --workpath "build" ^
  --icon "assets\iconswap.ico" ^
  --runtime-tmpdir "%ISW_TMPDIR%" ^
  --paths "src" ^
  --add-data "src\iconswap\templates;iconswap\templates" ^
  --add-data "src\iconswap\static;iconswap\static" ^
  --add-data "bin;bin" ^
  --add-data "assets;assets" ^
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
  --exclude-module numpy --exclude-module scipy --exclude-module pandas ^
  --exclude-module matplotlib --exclude-module tkinter --exclude-module PyQt5 ^
  --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 ^
  --exclude-module qtpy --exclude-module IPython --exclude-module pytest ^
  src\main.py
```

产物：仓库根目录的 `IconSwap.exe`（约 28 MB）。

要点：

- `--paths "src"`：让打包器找到 `iconswap` 包与 `ui_window` 模块；
- `--add-data ...;iconswap/...`：模板与静态资源随包带进 `_MEIPASS/iconswap/`，`ui_window._point_app_at_resources()` 会把 Flask 指过去；
- `--add-data "bin;bin"`：把 `rcedit.exe` 带进包内，运行时 `iconswap.paths.bin_dir()` 定位到 `_MEIPASS/bin`；
- `--runtime-tmpdir`：固定 onefile 解压目录（纯英文、普通用户可写），避免 `Could not create temporary directory!`；
- `--workpath build`：中间文件进 `build/`；`.spec` 会写在仓库根目录，已被 `.gitignore` 的 `*.spec` 忽略，无需提交。
  （不要加 `--specpath`：PyInstaller 会以 spec 所在目录为基准解析 `--add-data` 的相对路径，导致找不到 `src/iconswap/templates`。）

## 测试

```bat
pip install pytest
python -m pytest -q
```

- `tests/test_batch_replacer.py`：分流、降级、汇总、后台任务（注入假实现）
- `tests/test_two_functions.py`：两个功能的集成测试（含真实 COM 的空目标 `.lnk` 端到端）
- `tests/test_windows_tools.py`：真实 PE 签名、rcedit、快捷方式读写与校验、缓存刷新
- `tests/test_app.py` / `test_ico_converter.py` / `test_image_tools.py` / `test_ui_boot.py` / `test_risk_consent.py`

## 设计说明

**图标不落盘。** 「生成 ICO」的响应是纯字节流，程序不会把图标存到任何目录（历史版本里的 `IconsLibrary` 已移除）。批量替换使用的 `.ico` 必须是磁盘上真实存在的文件——因为 Windows 快捷方式只记录图标路径，指向临时文件的话程序一退出图标就白了。

**替换必须可验证。** 改 exe 后比对 PE 资源签名是否变化（识别"退出码 0 但其实没改"）；改快捷方式后重新读取 `IconLocation` 并比对路径与索引。任何不一致都会抛异常并出现在日志里，不会静默成功。

**改不动就降级，不硬来。** 数字签名、PyInstaller 单文件包、被占用的 exe 都会让 rcedit 失败或损坏文件，此时改为在桌面生成快捷方式；缺 `rcedit.exe` 属于环境问题，整批中止并提示。

**路径安全。** 调用 rcedit 用参数列表（不经过 shell），空格与中文路径原样传递；写 `IconLocation` 时使用**不带引号的绝对路径 + `,0`**——实测给图标路径加双引号会让资源管理器解析失败、显示成空白图标，所以空格/中文/逗号都不加引号（外壳按"最后一个逗号 + 数字"切分）。

## 第三方组件

见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。再分发 `IconSwap.exe` 时请保留 rcedit（MIT，© Electron）的版权与许可声明。

## 许可证

[MIT](LICENSE)
