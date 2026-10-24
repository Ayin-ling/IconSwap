rcedit.exe 已内置
=================

本目录已包含 rcedit v2.0.0（x64）：

    文件      : rcedit.exe
    大小      : 1,360,384 字节
    sha256    : 3e7801db1a5edbec91b49a24a094aad776cb4515488ea5a4ca2289c400eade2a
    来源      : https://github.com/electron/rcedit/releases/tag/v2.0.0
    命令行    : rcedit <exe> --set-icon <ico>

一般不需要改动。若要更新版本：从上面的 release 页下载 `rcedit-x64.exe`，
覆盖本目录的 `rcedit.exe` 即可；或用环境变量 `ICONSWAP_RCEDIT` 指向别的副本。

查找优先级（见 src/iconswap/paths.py）
--------------------------------------
1. 代码里显式传 `rcedit=...`
2. 环境变量 `ICONSWAP_RCEDIT`
3. 本目录 `bin\rcedit.exe`（开发时是仓库 bin\，打包后是 `_MEIPASS\bin\`）
4. 兼容旧布局的 `tools\rcedit.exe`、仓库根 `rcedit.exe`
5. 系统 `PATH`

打包
----
`build_exe.bat` 里的 `--add-data "bin;bin"` 会把本目录整体带进 exe，
用户机器上无需另装。

⚠️ 重要限制
-----------
rcedit 重写 exe 时会丢弃 PE 结构之后追加的数据。PyInstaller 单文件包、部分
自解压安装包的数据正是这样追加的——实测 27 MB 的包被削成 304 KB、程序损坏。
所以 `iconswap/resource_tools.replace_icon()` 会先检查尾部数据（`overlay_size`），
超过 1 MB 抛 `UnsafeOverlayError`，由上层自动降级为桌面快捷方式，不会破坏文件。
