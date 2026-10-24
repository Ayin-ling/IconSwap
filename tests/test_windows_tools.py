"""依赖操作系统的测试：真实 PE 签名检测、rcedit 定位、快捷方式读写、缓存刷新。

这些用例直接对着本机真实文件跑，能验证"真能工作"而不只是逻辑对不对。
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for _entry in (str(SRC), str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from iconswap.pe_inspector import (  # noqa: E402
    has_digital_signature,
    has_icon_resource,
    iter_executables,
    overlay_size,
    winverifytrust,
)
from iconswap.resource_tools import (  # noqa: E402
    ResourceToolError,
    ResourceToolMissing,
    UnsafeOverlayError,
    backup_path_for,
    locate_rcedit,
    replace_icon,
)
from iconswap.shell_cache import refresh_icon_cache  # noqa: E402
from iconswap.shortcut_tools import icon_parts as _icon_parts  # noqa: E402

TEMP_ROOT = ROOT / "tests" / "_tmp"

#: 本机已知带 Microsoft 签名的系统文件（找不到就跳过相关用例）
SIGNED_CANDIDATES = [
    Path(r"C:\Windows\System32\svchost.exe"),
    Path(r"C:\Windows\explorer.exe"),
    Path(r"C:\Windows\System32\rpcss.dll"),
]


@pytest.fixture()
def workdir():
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    directory = TEMP_ROOT / f"win-{uuid.uuid4().hex[:10]}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)


def _packaged_exe():
    """定位打包产物：项目根优先，其次旧版 dist 目录；都没有则返回 None。"""
    for candidate in (ROOT / "IconSwap.exe", ROOT / "dist" / "IconSwap.exe"):
        if candidate.is_file():
            return candidate
    return None


def _signed_sample() -> Path:
    for candidate in SIGNED_CANDIDATES:
        if candidate.is_file():
            info = has_digital_signature(candidate)
            if info.signed:
                return candidate
    pytest.skip("本机找不到带数字签名的样本文件")


# --------------------------------------------------------------------------- #
# 签名检测
# --------------------------------------------------------------------------- #
def test_signed_sample_is_detected():
    sample = _signed_sample()
    info = has_digital_signature(sample)

    assert info.signed is True
    assert info.has_security_directory is True
    assert info.cert_size > 8
    assert info.cert_type == 2  # WIN_CERT_TYPE_PKCS_SIGNED_DATA
    assert "已签名" in info.label


def test_signed_sample_passes_winverifytrust():
    sample = _signed_sample()
    assert winverifytrust(sample) is True


def test_our_own_exe_is_unsigned():
    # 本项目打包出来的 exe 没有签名（PyInstaller 默认不签）
    own = _packaged_exe()
    if own is None or not own.is_file():
        pytest.skip("尚未打包")
    info = has_digital_signature(own)
    assert info.signed is False
    assert info.label == "无签名"


def test_missing_and_non_pe_files_report_unsigned(workdir):
    missing = has_digital_signature(workdir / "nope.exe")
    assert missing.signed is False and missing.error

    bogus = workdir / "bogus.exe"
    bogus.write_bytes(b"not a pe file")
    info = has_digital_signature(bogus)
    assert info.signed is False
    assert info.error


def test_deep_check_keeps_fast_result_consistent():
    sample = _signed_sample()
    fast = has_digital_signature(sample)
    deep = has_digital_signature(sample, deep=True)

    assert fast.signed == deep.signed is True
    assert deep.verified is True


# --------------------------------------------------------------------------- #
# 枚举与图标资源
# --------------------------------------------------------------------------- #
def test_iter_executables_filters_and_sorts(workdir):
    (workdir / "b.exe").write_bytes(b"MZ")
    (workdir / "a.exe").write_bytes(b"MZ")
    (workdir / "a_original.exe").write_bytes(b"MZ")
    (workdir / "note.txt").write_text("x", encoding="utf-8")
    (workdir / "sub").mkdir()
    (workdir / "sub" / "c.exe").write_bytes(b"MZ")

    names = [path.name for path in iter_executables(workdir)]
    assert names == ["a.exe", "b.exe", "c.exe"]
    assert [p.name for p in iter_executables(workdir, recursive=False)] == ["a.exe", "b.exe"]


def test_iter_executables_rejects_file(workdir):
    target = workdir / "x.exe"
    target.write_bytes(b"MZ")
    with pytest.raises(NotADirectoryError):
        list(iter_executables(target))


def test_icon_resource_detection():
    # svchost 之类服务宿主可能没有图标资源，改用确定带图标的样本
    candidates = [_packaged_exe() or Path(), Path(r"C:\Windows\System32\notepad.exe")]
    for candidate in candidates:
        if candidate.is_file() and has_icon_resource(candidate):
            return
    pytest.skip("本机找不到带图标资源的样本")


# --------------------------------------------------------------------------- #
# rcedit 工具链
# --------------------------------------------------------------------------- #
def test_backup_path_naming(workdir):
    assert backup_path_for(workdir / "Demo.exe").name == "Demo_original.exe"


def test_locate_rcedit_reports_missing_with_hint(workdir, monkeypatch):
    """所有候选位置都不存在时，必须给出带路径提示的异常。"""
    import shutil as shutil_module
    from iconswap import paths as paths_module

    monkeypatch.setattr(paths_module, "rcedit_candidates", lambda: [])
    monkeypatch.setattr(shutil_module, "which", lambda *_: None)
    monkeypatch.delenv("ICONSWAP_RCEDIT", raising=False)

    with pytest.raises(ResourceToolMissing) as excinfo:
        locate_rcedit()

    message = str(excinfo.value)
    assert "rcedit.exe" in message
    assert "bin" in message


def test_replace_icon_rejects_non_ico(workdir):
    exe = workdir / "Demo.exe"
    _packaged = _packaged_exe()
    shutil.copy2(_packaged, exe) if _packaged else exe.write_bytes(b"MZ")
    png = workdir / "x.png"
    png.write_bytes(b"\x89PNG")

    with pytest.raises(ResourceToolError):
        replace_icon(exe, png, rcedit=workdir / "rcedit.exe")


# --------------------------------------------------------------------------- #
# 尾部数据防护（rcedit 会丢弃 PE 之后的附加数据，必须拒绝）
# --------------------------------------------------------------------------- #
def test_overlay_size_detects_pyinstaller_package():
    packed = _packaged_exe()
    if packed is None:
        pytest.skip("尚未打包")

    assert overlay_size(packed) > 1024 * 1024  # PyInstaller 归档追加在节之后
    native = Path(r"C:\Windows\System32\notepad.exe")
    if native.is_file():
        assert overlay_size(native) < 1024 * 1024


def test_replace_icon_refuses_packaged_exe(workdir):
    """单文件包必须被拒，且原文件与目录不产生任何改动。"""
    pytest.importorskip("pefile")
    packed = _packaged_exe()
    if packed is None:
        pytest.skip("尚未打包")

    exe = workdir / "Packaged.exe"
    shutil.copy2(packed, exe)
    before = exe.stat().st_size

    from PIL import Image

    ico = workdir / "brand.ico"
    Image.new("RGBA", (64, 64), (79, 70, 229, 255)).save(ico, format="ICO", sizes=[(16, 16)])

    with pytest.raises(UnsafeOverlayError):
        replace_icon(exe, ico)

    assert exe.stat().st_size == before
    assert not backup_path_for(exe).exists()


def test_replace_icon_works_on_native_exe(workdir):
    """普通原生 exe：替换成功、备份保留、图标资源存在。"""
    try:
        locate_rcedit()
    except ResourceToolMissing:
        pytest.skip("本机没有 rcedit.exe")

    sample = Path(r"C:\Windows\System32\notepad.exe")
    if not sample.is_file():
        pytest.skip("缺少可用的原生 exe 样本")

    exe = workdir / "Native.exe"
    shutil.copy2(sample, exe)
    original_size = exe.stat().st_size

    from PIL import Image

    ico = workdir / "brand.ico"
    Image.new("RGBA", (256, 256), (79, 70, 229, 255)).save(
        ico, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)]
    )

    backup = replace_icon(exe, ico)

    assert backup.is_file()
    assert backup.stat().st_size == original_size
    assert has_icon_resource(exe) is True
    assert abs(exe.stat().st_size - original_size) < 512 * 1024  # 体积不应剧变


# --------------------------------------------------------------------------- #
# 快捷方式（需要 pywin32 + COM）
# --------------------------------------------------------------------------- #
def test_shortcut_create_read_and_sync(workdir):
    pytest.importorskip("win32com.client")
    from iconswap.shortcut_tools import ShortcutTools, shortcut_name_for

    exe = workdir / "Demo.exe"
    source = _packaged_exe()
    if source is not None and source.is_file():
        shutil.copy2(source, exe)
    else:  # pragma: no cover - 未打包时的退化样本
        exe.write_bytes(b"MZ")

    from PIL import Image

    ico = workdir / "demo.ico"
    Image.new("RGBA", (32, 32), (79, 70, 229, 255)).save(ico, format="ICO", sizes=[(16, 16)])

    tools = ShortcutTools()
    lnk = tools.create(exe, workdir / shortcut_name_for(exe), icon=ico)
    info = tools.read(lnk)

    assert lnk.is_file()
    assert Path(info.target) == exe.resolve()
    assert _icon_parts(info.icon) == _icon_parts(str(ico.resolve()))

    assert [item.path.name for item in tools.find_targeting(exe, roots=[workdir])] == [lnk.name]
    assert [p.name for p in tools.sync_targeting(exe, None, roots=[workdir])] == [lnk.name]
    assert _icon_parts(tools.read(lnk).icon) == _icon_parts(str(exe.resolve()))


def test_shortcut_rejects_missing_target(workdir):
    pytest.importorskip("win32com.client")
    from iconswap.shortcut_tools import ShortcutError, ShortcutTools

    with pytest.raises(ShortcutError):
        ShortcutTools().create(workdir / "missing.exe", workdir / "x.lnk")


# --------------------------------------------------------------------------- #
# 图标缓存
# --------------------------------------------------------------------------- #
def test_refresh_icon_cache_reports_status(workdir):
    target = workdir / "Demo.exe"
    target.write_bytes(b"MZ")

    result = refresh_icon_cache([str(target)])

    assert result.notified is True
    assert result.ok is True
    assert "资源管理器" in result.as_text()

# --------------------------------------------------------------------------- #
# Bug 2 回归：快捷方式"静默失败"必须暴露
# --------------------------------------------------------------------------- #
class _FakeShortcut:
    """模拟 WScript.Shell 创建的快捷方式对象。"""

    def __init__(self, path, store):
        self._key = str(path)
        self._store = store
        data = store.get(self._key, {})
        self.TargetPath = data.get("target", "")
        self.IconLocation = data.get("icon", "")
        self.WorkingDirectory = ""
        self.Arguments = ""
        self.Description = ""

    def Save(self):
        if self._store.get("__noop__"):
            return  # 关键：静默失败，不抛异常也不落盘
        self._store[self._key] = {"target": self.TargetPath, "icon": self.IconLocation}


class _FakeShell:
    def __init__(self, noop: bool = False, seed: dict | None = None):
        self.store = {"__noop__": noop}
        if seed:
            self.store.update(seed)

    def CreateShortCut(self, path):
        return _FakeShortcut(path, self.store)


def _fake_lnk(workdir: Path) -> Path:
    lnk = workdir / "demo.lnk"
    lnk.write_bytes(b"fake")
    return lnk


def _fake_ico(workdir: Path) -> Path:
    from PIL import Image

    ico = workdir / "new.ico"
    Image.new("RGBA", (32, 32), (16, 185, 129, 255)).save(ico, format="ICO", sizes=[(16, 16)])
    return ico


def test_set_icon_raises_when_save_silently_does_nothing(workdir):
    """COM 不报错但没写进去 → 必须抛错并说明原因。"""
    from iconswap.shortcut_tools import ShortcutError, ShortcutTools

    lnk = _fake_lnk(workdir)
    ico = _fake_ico(workdir)
    tools = ShortcutTools(shell=_FakeShell(noop=True))

    with pytest.raises(ShortcutError) as excinfo:
        tools.set_icon(lnk, ico)

    message = str(excinfo.value)
    assert "修改快捷方式失败，可能被占用或权限不足" in message


def test_set_icon_succeeds_and_is_verified(workdir):
    from iconswap.shortcut_tools import ShortcutTools

    lnk = _fake_lnk(workdir)
    ico = _fake_ico(workdir)
    shell = _FakeShell()
    tools = ShortcutTools(shell=shell)

    result = tools.set_icon(lnk, ico)

    assert result == lnk
    assert "new.ico" in tools.read(lnk).icon
    assert ico.resolve().is_file()


def test_set_icon_reports_missing_file(workdir):
    from iconswap.shortcut_tools import ShortcutError, ShortcutTools

    with pytest.raises(ShortcutError):
        ShortcutTools(shell=_FakeShell()).set_icon(workdir / "missing.lnk", workdir / "x.ico")


def test_create_verifies_target_and_icon(workdir):
    from iconswap.shortcut_tools import ShortcutError, ShortcutTools

    exe = workdir / "app.exe"
    exe.write_bytes(b"MZ")
    ico = _fake_ico(workdir)

    shell = _FakeShell()
    tools = ShortcutTools(shell=shell)
    created = tools.create(exe, workdir / "ok.lnk", icon=ico)
    info = tools.read(created)
    assert Path(info.target) == exe.resolve()
    assert "new.ico" in info.icon

    # Save 被静默忽略时，写后校验必须报错
    with pytest.raises(ShortcutError):
        ShortcutTools(shell=_FakeShell(noop=True)).create(exe, workdir / "bad.lnk", icon=ico)


def test_icon_signature_normalizes_forms():
    from iconswap.shortcut_tools import icon_signature

    expected = icon_signature(r"C:\tools\a.ico")
    assert icon_signature(r"C:\tools\a.ico,0") == expected
    assert icon_signature('"C:\\tools\\a.ico",0') == expected
    assert icon_signature("c:/tools/a.ico") == expected
    assert icon_signature("") == ""


def test_icon_location_never_quotes_even_with_spaces():
    """回归：带双引号的 IconLocation 会让资源管理器显示空白图标。

    空格 / 中文 / 逗号都不能加引号，必须是裸绝对路径 + ``,0``。
    """
    from iconswap.shortcut_tools import icon_location_for

    plain = r"C:\icons\a.ico"
    spaced = r"C:\My Icons\我的 图标, v2.ico"

    assert icon_location_for(plain) == plain + ",0"
    assert icon_location_for(spaced) == spaced + ",0"
    assert '"' not in icon_location_for(spaced)
    assert icon_location_for(plain, 3).endswith(",3")


# --------------------------------------------------------------------------- #
# 权限判定
# --------------------------------------------------------------------------- #
def test_privilege_helpers():
    from iconswap.windows_privileges import is_admin, is_protected_path, permission_hint

    assert isinstance(is_admin(), bool)
    assert is_protected_path(r"C:\Program Files\App") is True
    assert is_protected_path(r"C:\Windows\System32") is True
    assert is_protected_path(r"C:\ProgramData\Tool") is True
    assert is_protected_path(r"D:\Tools\IconSwap") is False

    hint = permission_hint(r"C:\Program Files\App")
    if not is_admin():
        assert "管理员" in hint


def test_describe_com_error_falls_back_to_text():
    from iconswap.windows_privileges import describe_com_error

    assert "boom" in describe_com_error(RuntimeError("boom"))

def test_replace_icon_detects_silent_noop(workdir, monkeypatch):
    """rcedit 退出码 0 但没改动任何东西 → 必须报"替换未生效"并回滚。"""
    import types

    from iconswap import resource_tools

    sample = Path(r"C:\Windows\System32\notepad.exe")
    if not sample.is_file():
        pytest.skip("缺少可用的原生 exe 样本")

    exe = workdir / "Noop.exe"
    shutil.copy2(sample, exe)
    original = exe.read_bytes()

    from PIL import Image

    ico = workdir / "brand.ico"
    Image.new("RGBA", (16, 16), (1, 2, 3, 255)).save(ico, format="ICO")

    # 模拟"成功退出但什么都没改"
    monkeypatch.setattr(
        resource_tools.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with pytest.raises(ResourceToolError) as excinfo:
        replace_icon(exe, ico)

    assert "替换未生效" in str(excinfo.value)
    assert exe.read_bytes() == original, "失败后必须回滚到原文件"
