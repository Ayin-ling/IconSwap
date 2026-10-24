"""批量替换核心逻辑的单元测试（全部用注入的假实现，不碰真实 exe）。

覆盖点
------
* 扫描：只收 .exe、跳过 *_original.exe、递归/非递归、排除自身
* 分流：无签名 → 替换；已签名 + shortcut → 建快捷方式且**不调用编辑器**；
  已签名 + skip → 跳过
* 异常：文件被占用 → 占用跳过；rcedit 报错 → 失败；缺 rcedit → 中止整批
* 汇总文案、进度回调、缓存刷新、参数校验
* 后台任务：进度轮询、日志累积、错误状态
"""

from __future__ import annotations

import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from iconswap.batch_jobs import JobRegistry  # noqa: E402
from iconswap.icon_replacer import (  # noqa: E402
    ACTION_FAILED,
    ACTION_REPLACED,
    ACTION_SHORTCUT,
    ACTION_SKIPPED_LOCKED,
    ACTION_SHORTCUT_UPDATED,
    ACTION_SKIPPED,
    ACTION_SKIPPED_SIGNED,
    ACTION_SKIPPED_UNSAFE,
    POLICY_SHORTCUT,
    POLICY_SKIP,
    BatchIconReplacer,
)
from iconswap.pe_inspector import SignatureInfo  # noqa: E402
from iconswap.resource_tools import (  # noqa: E402
    ExeLockedError,
    ResourceToolError,
    ResourceToolMissing,
    UnsafeOverlayError,
)
from iconswap.shell_cache import RefreshResult  # noqa: E402

TEMP_ROOT = ROOT / "tests" / "_tmp"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def workdir():
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    directory = TEMP_ROOT / f"batch-{uuid.uuid4().hex[:10]}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ fake exe")
    return path


def _ico(workdir: Path) -> Path:
    from PIL import Image

    target = workdir / "new.ico"
    Image.new("RGBA", (32, 32), (79, 70, 229, 255)).save(
        target, format="ICO", sizes=[(16, 16), (32, 32)]
    )
    return target


class FakeInspector:
    """按文件名返回签名状态。"""

    def __init__(self, signed_names=()):
        self.signed_names = {name.lower() for name in signed_names}
        self.calls: list[Path] = []

    def __call__(self, path, deep=False):
        self.calls.append(Path(path))
        signed = Path(path).name.lower() in self.signed_names
        return SignatureInfo(
            signed=signed,
            has_security_directory=signed,
            cert_size=1024 if signed else 0,
            cert_type=2 if signed else None,
        )


class FakeEditor:
    def __init__(self, locked=(), errors=(), missing_tool=False, unsafe=()):
        self.locked = {Path(item).name for item in locked}
        self.errors = {Path(item).name for item in errors}
        self.unsafe = {Path(item).name for item in unsafe}
        self.missing_tool = missing_tool
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, exe, ico, **kwargs):
        exe = Path(exe)
        self.calls.append((exe, Path(ico)))
        if self.missing_tool:
            raise ResourceToolMissing("找不到 rcedit.exe")
        if exe.name in self.locked:
            raise ExeLockedError(f"{exe.name} 正在运行，已跳过")
        if exe.name in self.unsafe:
            raise UnsafeOverlayError(f"{exe.name} 在 PE 结构外附带 27.1 MB 数据，已跳过")
        if exe.name in self.errors:
            raise ResourceToolError("rcedit 失败（退出码 1）")
        backup = exe.with_name(f"{exe.stem}_original{exe.suffix}")
        backup.write_bytes(exe.read_bytes())
        return backup


class FakeShortcuts:
    def __init__(self, fail=False, targets=None, broken=(), fail_icon=()):
        self.created: list[tuple[Path, Path, Path]] = []
        self.synced: list[Path] = []
        self.icon_updates: list[tuple[Path, Path]] = []
        self.fail = fail
        #: lnk 路径(小写) -> 目标程序路径
        self.targets = {str(k).lower(): str(v) for k, v in (targets or {}).items()}
        self.broken = {str(item).lower() for item in broken}
        self.fail_icon = {str(item).lower() for item in fail_icon}

    def create(self, exe, lnk_path, icon=None, **kwargs):
        if self.fail:
            from iconswap.shortcut_tools import ShortcutError

            raise ShortcutError("模拟创建失败")
        lnk = Path(lnk_path)
        self.created.append((Path(exe), lnk, Path(icon) if icon else None))
        return lnk

    def sync_targeting(self, exe, icon=None, roots=None):
        self.synced.append(Path(exe))
        return [Path(f"{exe}.lnk")]

    def read(self, lnk_path):
        from iconswap.shortcut_tools import ShortcutError, ShortcutInfo

        key = str(lnk_path).lower()
        if key in self.broken:
            raise ShortcutError("模拟损坏的快捷方式")
        return ShortcutInfo(path=Path(lnk_path), target=self.targets.get(key, ""), icon="")

    def set_icon(self, lnk_path, icon, index=0):
        key = str(lnk_path).lower()
        if key in self.fail_icon:
            from iconswap.shortcut_tools import ShortcutError

            raise ShortcutError("模拟写入失败")
        self.icon_updates.append((Path(lnk_path), Path(icon)))
        return Path(lnk_path)


class FakeCache:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, paths=None, **kwargs):
        self.calls.append(list(paths or []))
        return RefreshResult(True, True, len(paths or []))


def _replacer(inspector, editor, shortcuts=None, cache=None):
    return BatchIconReplacer(
        inspector=inspector,
        editor=editor,
        shortcuts=shortcuts or FakeShortcuts(),
        cache=cache or FakeCache(),
    )


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def test_scan_collects_only_executables_and_skips_backups(workdir):
    _touch(workdir / "a.exe")
    _touch(workdir / "b.EXE")
    _touch(workdir / "a_original.exe")
    _touch(workdir / "readme.txt")
    _touch(workdir / "sub" / "c.exe")
    _touch(workdir / "sub" / "c_original.exe")

    replacer = _replacer(FakeInspector(), FakeEditor())
    names = [item.name for item in replacer.scan(workdir)]
    assert names == ["a.exe", "b.EXE", "c.exe"]

    flat = [item.name for item in replacer.scan(workdir, recursive=False)]
    assert flat == ["a.exe", "b.EXE"]


def test_scan_marks_signature_and_sorts(workdir):
    _touch(workdir / "z.exe")
    _touch(workdir / "signed.exe")
    inspector = FakeInspector(signed_names=["signed.exe"])

    planned = _replacer(inspector, FakeEditor()).scan(workdir)

    assert [item.name for item in planned] == ["signed.exe", "z.exe"]
    assert planned[0].signed is True
    assert planned[1].signed is False
    assert planned[0].signature.label.startswith("已签名")


def test_scan_rejects_missing_folder(workdir):
    with pytest.raises(NotADirectoryError):
        _replacer(FakeInspector(), FakeEditor()).scan(workdir / "nope")


# --------------------------------------------------------------------------- #
# 分流
# --------------------------------------------------------------------------- #
def test_unsigned_file_is_replaced_and_backed_up(workdir):
    exe = _touch(workdir / "app.exe")
    editor = FakeEditor()
    shortcuts = FakeShortcuts()
    cache = FakeCache()
    replacer = _replacer(FakeInspector(), editor, shortcuts, cache)

    summary = replacer.run(workdir, _ico(workdir))

    assert summary.replaced == 1
    assert len(editor.calls) == 1
    assert editor.calls[0][0] == exe
    assert summary.results[0].backup == exe.with_name("app_original.exe")
    assert summary.results[0].links_updated == 1
    assert shortcuts.synced == [exe]
    assert cache.calls and str(exe) in cache.calls[0]
    assert "成功替换 1 个" in summary.as_text()


def test_signed_file_gets_shortcut_and_is_never_edited(workdir):
    exe = _touch(workdir / "protected.exe")
    editor = FakeEditor()
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(signed_names=["protected.exe"]), editor, shortcuts)

    summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SHORTCUT)

    assert editor.calls == []  # 关键：绝不改签名文件
    assert summary.shortcuts_created == 1
    assert summary.results[0].action == ACTION_SHORTCUT
    exe_target, lnk_path, icon = shortcuts.created[0]
    assert exe_target == exe
    assert lnk_path.name == "protected - 快捷方式.lnk"
    from iconswap.shell_paths import desktop_dir

    assert lnk_path.parent == desktop_dir()
    assert icon is not None and icon.suffix == ".ico"


def test_signed_file_is_skipped_when_policy_is_skip(workdir):
    _touch(workdir / "protected.exe")
    editor = FakeEditor()
    replacer = _replacer(FakeInspector(signed_names=["protected.exe"]), editor)

    summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SKIP)

    assert editor.calls == []
    assert summary.skipped_signed == 1
    assert summary.results[0].action == ACTION_SKIPPED_SIGNED
    assert "受数字签名保护" in summary.results[0].message
    assert "因签名跳过 1 个" in summary.as_text()


def test_signed_shortcut_uses_custom_directory(workdir):
    _touch(workdir / "protected.exe")
    out_dir = workdir / "links"
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(signed_names=["protected.exe"]), FakeEditor(), shortcuts)

    replacer.run(workdir, _ico(workdir), policy=POLICY_SHORTCUT, shortcut_dir=out_dir)

    assert shortcuts.created[0][1].parent == out_dir.resolve()


def test_signed_shortcut_failure_is_reported(workdir):
    _touch(workdir / "protected.exe")
    replacer = _replacer(
        FakeInspector(signed_names=["protected.exe"]),
        FakeEditor(),
        FakeShortcuts(fail=True),
    )

    summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SHORTCUT)

    assert summary.failed == 1
    assert "创建快捷方式失败" in summary.results[0].message


# --------------------------------------------------------------------------- #
# 异常与边界
# --------------------------------------------------------------------------- #
def test_locked_file_degrades_to_shortcut(workdir):
    """正在运行的 exe 改不了 → 自动降级为桌面快捷方式（默认策略）。"""
    _touch(workdir / "busy.exe")
    _touch(workdir / "free.exe")
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(), FakeEditor(locked=["busy.exe"]), shortcuts)

    summary = replacer.run(workdir, _ico(workdir))

    assert summary.replaced == 1
    assert summary.shortcuts_created == 1
    degraded = next(item for item in summary.results if item.path.name == "busy.exe")
    assert degraded.action == ACTION_SHORTCUT
    assert degraded.degraded is True
    assert "无法直接修改" in degraded.message
    assert "正在运行" in degraded.message
    assert shortcuts.created[0][0].name == "busy.exe"


def test_locked_file_is_skipped_when_policy_is_skip(workdir):
    _touch(workdir / "busy.exe")
    replacer = _replacer(FakeInspector(), FakeEditor(locked=["busy.exe"]))

    summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SKIP)

    assert summary.replaced == 0
    assert summary.skipped_locked == 1
    assert summary.shortcuts_created == 0
    assert "正在运行" in summary.results[0].message


def test_unsafe_overlay_degrades_to_shortcut(workdir):
    """结构保护（PE 外挂数据）不再直接放弃：默认策略下自动建快捷方式。"""
    packed = _touch(workdir / "packed.exe")
    editor = FakeEditor(unsafe=["packed.exe"])
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(), editor, shortcuts)

    summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SHORTCUT)

    assert len(editor.calls) == 1                  # 编辑器尝试过但被拒绝
    assert summary.results[0].action == ACTION_SHORTCUT
    assert summary.results[0].degraded is True
    message = summary.results[0].message
    assert "无法直接修改" in message
    assert "已创建桌面快捷方式并替换图标" in message
    assert "PE 结构外附带" in message
    assert shortcuts.created[0][0] == packed        # 快捷方式指向该 exe
    assert summary.shortcuts_created == 1
    assert summary.degraded == 1
    assert "因 exe 改不了而自动降级" in summary.as_text()


def test_unsafe_overlay_respects_skip_policy(workdir):
    """策略为“跳过”时，结构保护文件才真正跳过。"""
    _touch(workdir / "packed.exe")
    editor = FakeEditor(unsafe=["packed.exe"])
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(), editor, shortcuts)

    summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SKIP)

    assert summary.results[0].action == ACTION_SKIPPED_UNSAFE
    assert shortcuts.created == []
    assert summary.degraded == 0
    assert "当前策略为“跳过”" in summary.results[0].message


def test_degraded_shortcut_creation_failure_is_reported(workdir):
    _touch(workdir / "packed.exe")
    replacer = _replacer(
        FakeInspector(), FakeEditor(unsafe=["packed.exe"]), FakeShortcuts(fail=True)
    )

    summary = replacer.run(workdir, _ico(workdir))

    assert summary.failed == 1
    assert "降级创建快捷方式失败" in summary.results[0].message


def test_mixed_batch_counts(tmp_path=None):
    """无签名 exe 正常替换、结构保护 exe 降级、签名 exe 建快捷方式，互不干扰。"""
    import uuid as _uuid

    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = TEMP_ROOT / f"mixed-{_uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True)
    try:
        _touch(workdir / "plain.exe")
        _touch(workdir / "packed.exe")
        _touch(workdir / "signed.exe")
        shortcuts = FakeShortcuts()
        replacer = _replacer(
            FakeInspector(signed_names=["signed.exe"]),
            FakeEditor(unsafe=["packed.exe"]),
            shortcuts,
        )

        summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SHORTCUT)

        assert summary.replaced == 1
        assert summary.degraded == 1
        assert summary.shortcuts_created == 2
        assert summary.failed == 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_editor_error_degrades_to_shortcut(workdir):
    """rcedit 报错（退出码非 0）不再直接失败：自动降级为桌面快捷方式。"""
    _touch(workdir / "broken.exe")
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(), FakeEditor(errors=["broken.exe"]), shortcuts)

    summary = replacer.run(workdir, _ico(workdir))

    assert summary.failed == 0
    assert summary.shortcuts_created == 1
    assert summary.results[0].action == ACTION_SHORTCUT
    assert "rcedit 失败" in summary.results[0].message
    assert summary.aborted is False


def test_editor_error_respects_skip_policy(workdir):
    _touch(workdir / "broken.exe")
    replacer = _replacer(FakeInspector(), FakeEditor(errors=["broken.exe"]))

    summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SKIP)

    assert summary.failed == 0
    assert summary.shortcuts_created == 0
    assert summary.results[0].action == ACTION_SKIPPED
    assert "rcedit 失败" in summary.results[0].message


def test_missing_rcedit_aborts_batch(workdir):
    _touch(workdir / "a.exe")
    _touch(workdir / "b.exe")
    replacer = _replacer(FakeInspector(), FakeEditor(missing_tool=True))

    summary = replacer.run(workdir, _ico(workdir))

    assert summary.aborted is True
    assert summary.failed == 1
    assert len(summary.results) == 1  # 第一个就中止，不再继续
    assert "rcedit" in summary.results[0].message


def test_progress_callback_reports_each_file(workdir):
    _touch(workdir / "a.exe")
    _touch(workdir / "b.exe")
    seen: list[tuple[int, int, str]] = []
    replacer = _replacer(FakeInspector(), FakeEditor())

    replacer.run(
        workdir,
        _ico(workdir),
        progress=lambda index, total, result: seen.append((index, total, result.action)),
    )

    assert seen == [(1, 2, ACTION_REPLACED), (2, 2, ACTION_REPLACED)]


# --------------------------------------------------------------------------- #
# 快捷方式 (.lnk) 支持
# --------------------------------------------------------------------------- #
def _touch_lnk(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-lnk")
    return path


def test_scan_includes_shortcuts_with_target_and_signature(workdir):
    target = _touch(workdir / "signed.exe")
    lnk = _touch_lnk(workdir / "signed - 快捷方式.lnk")
    shortcuts = FakeShortcuts(targets={lnk: target})
    replacer = _replacer(
        FakeInspector(signed_names=["signed.exe"]), FakeEditor(), shortcuts
    )

    planned = replacer.scan(workdir)

    kinds = {item.name: item.kind for item in planned}
    assert kinds == {"signed.exe": "exe", "signed - 快捷方式.lnk": "lnk"}
    shortcut = next(item for item in planned if item.kind == "lnk")
    assert shortcut.target == target
    assert shortcut.signed is True


def test_include_lnk_false_ignores_shortcuts(workdir):
    _touch(workdir / "a.exe")
    _touch_lnk(workdir / "a.lnk")

    planned = _replacer(FakeInspector(), FakeEditor()).scan(workdir, include_lnk=False)

    assert [item.kind for item in planned] == ["exe"]


def test_shortcut_icon_is_updated_without_touching_target(workdir):
    target = _touch(workdir / "signed.exe")
    lnk = _touch_lnk(workdir / "signed.lnk")
    shortcuts = FakeShortcuts(targets={lnk: target})
    editor = FakeEditor()
    replacer = _replacer(
        FakeInspector(signed_names=["signed.exe"]), editor, shortcuts
    )
    ico = _ico(workdir)

    summary = replacer.run(workdir, ico)

    assert editor.calls == [], "改快捷方式时绝不能碰目标 exe"
    assert summary.shortcuts_updated == 1
    result = next(item for item in summary.results if item.action == ACTION_SHORTCUT_UPDATED)
    assert result.message == "已成功修改快捷方式图标：signed.lnk"
    assert shortcuts.icon_updates == [(lnk, ico)]
    assert "已更新快捷方式图标 1 个" in summary.as_text()
    assert summary.to_dict()["shortcuts_updated"] == 1


def test_shortcut_is_processed_even_with_skip_policy(workdir):
    target = _touch(workdir / "signed.exe")
    lnk = _touch_lnk(workdir / "signed.lnk")
    shortcuts = FakeShortcuts(targets={lnk: target})
    replacer = _replacer(
        FakeInspector(signed_names=["signed.exe"]), FakeEditor(), shortcuts
    )

    summary = replacer.run(workdir, _ico(workdir), policy=POLICY_SKIP)

    # 跳过策略只针对 exe；.lnk 改的是快捷方式本身，仍然要处理
    assert summary.skipped_signed == 1
    assert summary.shortcuts_updated == 1
    assert len(shortcuts.icon_updates) == 1


def test_broken_shortcut_is_skipped_not_fatal(workdir):
    good = _touch(workdir / "a.exe")
    bad = _touch_lnk(workdir / "broken.lnk")
    shortcuts = FakeShortcuts(broken=[bad])
    replacer = _replacer(FakeInspector(), FakeEditor(), shortcuts)

    planned = replacer.scan(workdir)
    broken = next(item for item in planned if item.kind == "lnk")
    assert "无法解析快捷方式" in broken.note

    summary = replacer.run(workdir, _ico(workdir))

    result = next(item for item in summary.results if item.path == bad)
    assert result.action == ACTION_SKIPPED
    assert "无法解析快捷方式" in result.message
    assert summary.replaced == 1  # 正常的 exe 仍被处理
    assert summary.failed == 0


def test_shortcut_without_target_is_still_updated(workdir):
    """核心 bug 回归：TargetPath 为空的 .lnk 也必须照改图标，不许跳过。

    商店 / UWP 应用、目标已被删除的快捷方式都属于这一类。
    """
    lnk = _touch_lnk(workdir / "no-target.lnk")
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(), FakeEditor(), shortcuts)

    planned = replacer.scan(workdir)
    assert planned[0].note == ""            # 不再标记为“没有目标程序”
    assert planned[0].target is None
    assert planned[0].kind == "lnk"

    summary = replacer.run(workdir, _ico(workdir))

    assert summary.results[0].action == ACTION_SHORTCUT_UPDATED
    assert shortcuts.icon_updates == [(lnk, _ico(workdir))]
    assert summary.shortcuts_created == 0   # 绝不额外新建快捷方式
    assert "没有目标程序" in summary.results[0].message


def test_shortcut_with_empty_icon_location_is_updated(workdir):
    """IconLocation 原本为空的快捷方式同样要写入并校验。"""
    lnk = _touch_lnk(workdir / "empty-icon.lnk")
    shortcuts = FakeShortcuts(targets={lnk: ""})
    replacer = _replacer(FakeInspector(), FakeEditor(), shortcuts)

    summary = replacer.run(workdir, _ico(workdir))

    assert summary.results[0].action == ACTION_SHORTCUT_UPDATED
    assert shortcuts.icon_updates


def test_shortcut_write_failure_is_reported(workdir):
    target = _touch(workdir / "a.exe")
    lnk = _touch_lnk(workdir / "a.lnk")
    shortcuts = FakeShortcuts(targets={lnk: target}, fail_icon=[lnk])
    replacer = _replacer(FakeInspector(), FakeEditor(), shortcuts)

    summary = replacer.run(workdir, _ico(workdir))

    result = next(item for item in summary.results if item.path == lnk)
    assert result.action == ACTION_FAILED
    assert "改快捷方式失败" in result.message


def test_scan_sorts_exe_and_lnk_together(workdir):
    _touch(workdir / "b.exe")
    _touch_lnk(workdir / "a.lnk")

    planned = _replacer(FakeInspector(), FakeEditor()).scan(workdir)

    assert [item.name for item in planned] == ["a.lnk", "b.exe"]


def test_invalid_policy_raises(workdir):
    _touch(workdir / "a.exe")
    with pytest.raises(ValueError):
        _replacer(FakeInspector(), FakeEditor()).run(workdir, _ico(workdir), policy="whatever")


def test_missing_ico_raises(workdir):
    _touch(workdir / "a.exe")
    with pytest.raises(FileNotFoundError):
        _replacer(FakeInspector(), FakeEditor()).run(workdir, workdir / "nope.ico")


def test_refresh_can_be_disabled(workdir):
    _touch(workdir / "a.exe")
    cache = FakeCache()
    replacer = _replacer(FakeInspector(), FakeEditor(), cache=cache)

    summary = replacer.run(workdir, _ico(workdir), refresh=False)

    assert cache.calls == []
    assert summary.refresh is None


def test_summary_to_dict_is_json_ready(workdir):
    _touch(workdir / "a.exe")
    summary = _replacer(FakeInspector(), FakeEditor()).run(workdir, _ico(workdir))
    payload = summary.to_dict()

    assert payload["replaced"] == 1
    assert payload["results"][0]["action"] == ACTION_REPLACED
    assert isinstance(payload["summary"], str)
    assert payload["refresh"]["notified"] is True


def test_original_files_are_not_reprocessed(workdir):
    """连续跑两次时，第一次生成的 *_original.exe 不该被再次替换。"""
    exe = _touch(workdir / "app.exe")
    editor = FakeEditor()
    replacer = _replacer(FakeInspector(), editor)

    replacer.run(workdir, _ico(workdir))
    (workdir / "app_original.exe").write_bytes(b"MZ backup")
    replacer.run(workdir, _ico(workdir))

    assert all(call[0] == exe for call in editor.calls)


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #
def _wait(job_getter, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = job_getter(job_id)
        if job is not None and job.state != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("任务超时未结束")


def test_job_registry_runs_and_reports(workdir):
    _touch(workdir / "a.exe")
    _touch(workdir / "b.exe")
    ico = _ico(workdir)
    registry = JobRegistry()
    replacer = _replacer(FakeInspector(), FakeEditor())

    job = registry.start(workdir, ico, replacer_factory=lambda: replacer)
    finished = _wait(registry.get, job.id)

    assert finished.state == "done"
    assert finished.total == 2
    assert finished.done == 2
    payload = finished.to_dict()
    assert payload["result"]["replaced"] == 2
    assert len(payload["logs"]) >= 3
    assert "成功替换 2 个" in payload["logs"][-1]


def test_job_registry_marks_error_when_rcedit_missing(workdir):
    _touch(workdir / "a.exe")
    registry = JobRegistry()
    replacer = _replacer(FakeInspector(), FakeEditor(missing_tool=True))

    job = registry.start(workdir, _ico(workdir), replacer_factory=lambda: replacer)
    finished = _wait(registry.get, job.id)

    assert finished.state == "error"
    assert "rcedit" in (finished.error or "")


def test_job_registry_rejects_bad_policy(workdir):
    registry = JobRegistry()
    with pytest.raises(ValueError):
        registry.start(workdir, "x.ico", policy="nope")


def test_job_registry_keeps_bounded_history(workdir):
    _touch(workdir / "a.exe")
    registry = JobRegistry(max_jobs=2)
    replacer = _replacer(FakeInspector(), FakeEditor())
    ico = _ico(workdir)

    first = registry.start(workdir, ico, replacer_factory=lambda: replacer)
    _wait(registry.get, first.id)
    second = registry.start(workdir, ico, replacer_factory=lambda: replacer)
    _wait(registry.get, second.id)
    third = registry.start(workdir, ico, replacer_factory=lambda: replacer)
    _wait(registry.get, third.id)

    assert registry.get(first.id) is None
    assert registry.get(third.id) is not None

# --------------------------------------------------------------------------- #
# 接口层：扫描要同时报告 .exe 与 .lnk
# --------------------------------------------------------------------------- #
def test_scan_api_reports_exe_and_shortcut(workdir):
    from iconswap.app import create_app

    _touch(workdir / "app.exe")
    _touch_lnk(workdir / "app - 快捷方式.lnk")

    client = create_app().test_client()
    response = client.post("/api/batch/scan", json={"folder": str(workdir)})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["exe_count"] == 1
    assert payload["lnk_count"] == 1
    assert payload["total"] == 2
    kinds = {item["kind"] for item in payload["files"]}
    assert kinds == {"exe", "lnk"}


def test_scan_api_can_exclude_shortcuts(workdir):
    from iconswap.app import create_app

    _touch(workdir / "app.exe")
    _touch_lnk(workdir / "app.lnk")

    client = create_app().test_client()
    response = client.post(
        "/api/batch/scan", json={"folder": str(workdir), "include_lnk": False}
    )

    payload = response.get_json()
    assert payload["lnk_count"] == 0
    assert payload["total"] == 1


def test_scan_api_survives_corrupt_shortcut(workdir, monkeypatch):
    """真正读不出来的 .lnk 只能标记为跳过，不能把扫描接口打挂。"""
    from iconswap.app import create_app
    from iconswap.shortcut_tools import ShortcutError, ShortcutTools

    def boom(self, lnk_path):
        raise ShortcutError("模拟损坏的快捷方式")

    monkeypatch.setattr(ShortcutTools, "read", boom)

    _touch_lnk(workdir / "broken.lnk")

    client = create_app().test_client()
    response = client.post("/api/batch/scan", json={"folder": str(workdir)})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["lnk_count"] == 1
    assert payload["files"][0]["note"]

# --------------------------------------------------------------------------- #
# 功能区 2：扫描模式（exe+lnk / 仅 lnk）
# --------------------------------------------------------------------------- #
def test_scan_mode_lnk_only(workdir):
    _touch(workdir / "app.exe")
    _touch_lnk(workdir / "app.lnk")

    planned = _replacer(FakeInspector(), FakeEditor()).scan(workdir, mode="lnk")

    assert [item.kind for item in planned] == ["lnk"]


def test_scan_mode_all_includes_both(workdir):
    _touch(workdir / "app.exe")
    _touch_lnk(workdir / "app.lnk")

    planned = _replacer(FakeInspector(), FakeEditor()).scan(workdir, mode="all")

    assert sorted(item.kind for item in planned) == ["exe", "lnk"]


def test_scan_mode_rejects_unknown(workdir):
    with pytest.raises(ValueError):
        _replacer(FakeInspector(), FakeEditor()).scan(workdir, mode="whatever")


def test_scan_api_mode_lnk(workdir):
    from iconswap.app import create_app

    _touch(workdir / "app.exe")
    _touch_lnk(workdir / "app.lnk")

    client = create_app().test_client()
    response = client.post(
        "/api/batch/scan", json={"folder": str(workdir), "mode": "lnk"}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["mode"] == "lnk"
    assert payload["exe_count"] == 0
    assert payload["lnk_count"] == 1
    assert payload["total"] == 1


def test_scan_api_rejects_bad_mode(workdir):
    from iconswap.app import create_app

    client = create_app().test_client()
    response = client.post("/api/batch/scan", json={"folder": str(workdir), "mode": "x"})

    assert response.status_code == 400
    assert "mode" in response.get_json()["error"]


# --------------------------------------------------------------------------- #
# 功能区 3：只处理被勾选的文件
# --------------------------------------------------------------------------- #
def test_only_selected_paths_are_processed(workdir):
    keep = _touch(workdir / "keep.exe")
    skip = _touch(workdir / "skip.exe")
    editor = FakeEditor()
    replacer = _replacer(FakeInspector(), editor)

    summary = replacer.run(workdir, _ico(workdir), only_paths=[str(keep)])

    assert [call[0] for call in editor.calls] == [keep]
    assert summary.scanned == 1
    assert summary.replaced == 1


def test_only_paths_accepts_relative_and_absolute(workdir, monkeypatch):
    keep = _touch(workdir / "keep.exe")
    _touch(workdir / "skip.exe")
    editor = FakeEditor()
    replacer = _replacer(FakeInspector(), editor)

    summary = replacer.run(workdir, _ico(workdir), only_paths=[keep])

    assert summary.replaced == 1
    assert [call[0] for call in editor.calls] == [keep]


def test_only_paths_empty_means_nothing(workdir):
    _touch(workdir / "a.exe")
    editor = FakeEditor()
    replacer = _replacer(FakeInspector(), editor)

    summary = replacer.run(workdir, _ico(workdir), only_paths=[])

    assert editor.calls == []
    assert summary.results == []
    assert summary.scanned == 0


def test_only_paths_with_mixed_kinds(workdir):
    exe = _touch(workdir / "app.exe")
    lnk = _touch_lnk(workdir / "app.lnk")
    shortcuts = FakeShortcuts(targets={lnk: exe})
    replacer = _replacer(FakeInspector(), FakeEditor(), shortcuts)

    summary = replacer.run(workdir, _ico(workdir), only_paths=[str(lnk)])

    assert summary.shortcuts_updated == 1
    assert summary.replaced == 0


def test_run_mode_lnk_only(workdir):
    _touch(workdir / "app.exe")
    lnk = _touch_lnk(workdir / "app.lnk")
    shortcuts = FakeShortcuts(targets={lnk: _touch(workdir / "target.exe")})
    editor = FakeEditor()
    replacer = _replacer(FakeInspector(), editor, shortcuts)

    summary = replacer.run(workdir, _ico(workdir), mode="lnk")

    assert editor.calls == []          # 不碰 exe
    assert summary.shortcuts_updated == 1
    assert summary.scanned == 1

def test_protected_dir_hint_is_shown_in_log(workdir):
    """受保护目录 + 非管理员时，权限提示要出现在 UI 日志行里。"""
    target = _touch(workdir / "a.exe")
    lnk = _touch_lnk(workdir / "a.lnk")

    class HintShortcuts(FakeShortcuts):
        def __init__(self):
            super().__init__(targets={lnk: target})
            self.last_hint = ""

        def set_icon(self, lnk_path, icon, index=0):
            self.last_hint = "目标位于受保护目录，建议以管理员身份运行"
            return super().set_icon(lnk_path, icon, index)

    replacer = _replacer(FakeInspector(), FakeEditor(), HintShortcuts())

    summary = replacer.run(workdir, _ico(workdir))

    result = next(item for item in summary.results if item.path == lnk)
    assert "已成功修改快捷方式图标" in result.message
    assert "建议以管理员身份运行" in result.message

# --------------------------------------------------------------------------- #
# 图标来源：必须是磁盘上的真实 .ico（图标库已移除，不做任何拷贝/缓存）
# --------------------------------------------------------------------------- #
def test_resolve_ico_accepts_absolute_path(workdir):
    from iconswap.batch_api import _resolve_ico
    from iconswap.app import create_app

    ico = _ico(workdir)
    client = create_app().test_client()
    with client.application.test_request_context(
        "/api/batch/start", json={"ico_path": str(ico)}
    ):
        assert _resolve_ico() == ico.resolve()


def test_resolve_ico_rejects_missing_path(workdir):
    from iconswap.batch_api import _resolve_ico
    from iconswap.app import create_app

    client = create_app().test_client()
    with client.application.test_request_context(
        "/api/batch/start", json={"ico_path": str(workdir / "nope.ico")}
    ):
        with pytest.raises(ValueError) as excinfo:
            _resolve_ico()
    assert "不存在" in str(excinfo.value)


def test_resolve_ico_rejects_non_ico(workdir):
    from iconswap.batch_api import _resolve_ico
    from iconswap.app import create_app

    png = workdir / "x.png"
    png.write_bytes(b"\x89PNG")
    client = create_app().test_client()
    with client.application.test_request_context(
        "/api/batch/start", json={"ico_path": str(png)}
    ):
        with pytest.raises(ValueError) as excinfo:
            _resolve_ico()
    assert ".ico" in str(excinfo.value)


def test_shortcut_points_to_the_real_icon_file(workdir):
    """降级创建的快捷方式，IconLocation 必须指向用户真实选中的 ico。"""
    _touch(workdir / "packed.exe")
    ico = _ico(workdir)

    shortcuts = FakeShortcuts()
    replacer = _replacer(
        FakeInspector(), FakeEditor(unsafe=["packed.exe"]), shortcuts
    )

    summary = replacer.run(workdir, ico)

    assert summary.results[0].degraded is True
    used_icon = shortcuts.created[0][2]
    assert used_icon == ico
    assert used_icon.is_absolute()
    assert used_icon.is_file()


def test_summary_wording_when_only_shortcuts(workdir):
    """只建快捷方式时，不能说"成功替换 0 个"。"""
    _touch(workdir / "packed.exe")
    replacer = _replacer(FakeInspector(), FakeEditor(unsafe=["packed.exe"]))

    summary = replacer.run(workdir, _ico(workdir))

    text = summary.as_text()
    assert "成功替换 0 个" not in text
    assert "已创建并替换快捷方式 1 个" in text

# --------------------------------------------------------------------------- #
# 快捷方式位置：默认桌面，可显式指定
# --------------------------------------------------------------------------- #
def test_shortcut_goes_to_desktop_by_default(workdir):
    from iconswap.shell_paths import desktop_dir

    exe = _touch(workdir / "app.exe")
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(signed_names=["app.exe"]), FakeEditor(), shortcuts)

    replacer.run(workdir, _ico(workdir))

    created = shortcuts.created[0][1]
    assert created.parent == desktop_dir()
    assert created.name == "app - 快捷方式.lnk"


def test_shortcut_dir_override_is_respected(workdir):
    target_dir = workdir / "links"
    exe = _touch(workdir / "app.exe")
    shortcuts = FakeShortcuts()
    replacer = _replacer(FakeInspector(signed_names=["app.exe"]), FakeEditor(), shortcuts)

    replacer.run(workdir, _ico(workdir), shortcut_dir=target_dir)

    created = shortcuts.created[0][1]
    assert created.parent == target_dir.resolve()


def test_shortcut_falls_back_when_desktop_unwritable(workdir):
    """桌面写不进去时，退到 exe 同级并在日志里说明。"""
    exe = _touch(workdir / "app.exe")

    class DesktopFails(FakeShortcuts):
        def create(self, exe_path, lnk_path, icon=None, **kwargs):
            if lnk_path.parent.name.lower() == "desktop":
                from iconswap.shortcut_tools import ShortcutError

                raise ShortcutError("模拟桌面不可写")
            return super().create(exe_path, lnk_path, icon=icon, **kwargs)

    shortcuts = DesktopFails()
    replacer = _replacer(FakeInspector(signed_names=["app.exe"]), FakeEditor(), shortcuts)

    summary = replacer.run(workdir, _ico(workdir))

    created = shortcuts.created[0][1]
    assert created.parent == exe.parent
    assert "程序目录" in summary.results[0].message
