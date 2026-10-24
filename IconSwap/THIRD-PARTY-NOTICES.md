# 第三方组件与许可证声明

IconSwap 使用了下列第三方组件。各组件的版权与许可证归其作者所有，本文件仅作
汇总说明，不改变各组件原有许可条款。

| 组件 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| [rcedit](https://github.com/electron/rcedit) | 2.0.0 | MIT（© Electron） | 替换 exe 内嵌图标资源 |
| [pywebview](https://github.com/r0x0r/pywebview) | 见 `pip show` | BSD-3-Clause | 桌面窗口（WebView2 宿主） |
| [pythonnet](https://github.com/pythonnet/pythonnet) | 见 `pip show` | MIT | pywebview 的 .NET 互操作 |
| [Flask](https://github.com/pallets/flask) | 见 `pip show` | BSD-3-Clause | 本地 HTTP 服务与路由 |
| [Werkzeug](https://github.com/pallets/werkzeug) | 见 `pip show` | BSD-3-Clause | WSGI 服务器 |
| [Pillow](https://python-pillow.org) | 见 `pip show` | MIT-CMU (HPND) | 图片解码与 ICO 编码 |
| [pefile](https://github.com/erocarrera/pefile) | 见 `pip show` | MIT | PE 解析与签名检测 |
| [pywin32](https://github.com/mhammond/pywin32) | 见 `pip show` | PSF-2.0 | 快捷方式读写、Win32 API |
| [PyInstaller](https://pyinstaller.org) | 见 `pip show` | GPL-2.0-or-later WITH bootloader-exception | 打包（仅构建期） |

实际运行版本可在程序「关于」中查看（`GET /api/about` 会返回真实版本号）。

## rcedit

```
rcedit - Edit resources of exe.
Copyright (c) Electron contributors
Licensed under the MIT License.
```

`bin\rcedit.exe` 由 `build_exe.bat` 的 `--add-data "bin;bin"` 一并打包进
`IconSwap.exe`，因此分发本程序的二进制产物时，请保留上述版权与许可声明。

完整 MIT 许可证文本：https://opensource.org/licenses/MIT

## 免责声明

- 图标替换会修改目标可执行文件。程序会自动生成 `原名_original.exe` 备份，
  但请自行确认备份可还原后再分发或删除。
- 带数字签名的程序不会被修改（只创建快捷方式）；若你通过代码参数强行改写，
  签名会失效，可能导致系统或杀软拦截。
- 对带 PE 尾部附加数据的程序（PyInstaller/自解压单文件包）程序会拒绝替换，
  因为底层工具会丢弃这段数据、破坏程序。
