"""两个独立功能的集成测试。

* 功能一 ``mode=exe``：只扫 exe，失败自动降级为桌面快捷方式；
* 功能二 ``mode=lnk``：只改被选中的快捷方式，**不看目标、绝不新建**。

这里的用例覆盖了历史 bug：“快捷方式没有目标程序 → 跳过”。
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from iconswap.icon_replacer import (  # noqa: E402
    ACTION_SHORTCUT,
    ACTION_SHORTCUT_UPDATED,
    BatchIconReplacer,
)
from iconswap.pe_inspector import SignatureInfo  # noqa: E402
from iconswap.resource_tools import ExeLockedError, ResourceToolError  # noqa: E402
from iconswap.shell_cache import RefreshResult  # noqa: E402
from iconswap.shortcut_tools import ShortcutInfo, icon_location_for, icon_parts  # noqa: E402

TEMP_ROOT = ROOT / "tests" / "_tmp_modes"


@pytest.fixture()
def workdir():
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    directory = TEMP_ROOT / f"modes-{uuid.uuid4().hex[:10]}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_root():
    """跑完把临时根目录也清掉，不在项目里留空文件夹。"""
    yield
    import shutil

    shutil.rmtree(TEMP_ROOT, ignore_errors=True)


def _touch(path: Path, data: bytes = b"MZ fake exe") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _ico(directory: Path) -> Path:
    target = directory / "icon.ico"
    Image.new("RGBA", (32, 32), (79, 70, 229, 255)).save(
        target, format="ICO", sizes=[(16, 16), (32, 32)]
    )
    return target


class _Inspector:
    def __call__(self, path, deep=False):
        return SignatureInfo(signed=False)


class _Editor:
    def __init__(self, locked=(), errors=()):
        self.locked = set(locked)
        self.errors = set(errors)
        self.calls = []

    def __call__(self, exe, ico, **kwargs):
        exe = Path(exe)
        self.calls.append(exe)
        if exe.name in self.locked:
            raise ExeLockedError(f"{exe.name} 正在运行")
        if exe.name in self.errors:
            raise ResourceToolError("rcedit 失败（退出码 1）")
        return exe


class _Shortcuts:
    def __init__(self, targets=None):
        self.created = []
        self.icon_updates = []
        self.targets = {str(k).lower(): v for k, v in (targets or {}).items()}
        self.last_hint = ""

    def read(self, lnk_path):
        key = str(lnk_path).lower()
        return ShortcutInfo(path=Path(lnk_path), target=self.targets.get(key, ""), icon="")

    def set_icon(self, lnk_path, icon, index=0):
        self.icon_updates.append((Path(lnk_path), Path(icon)))
        return Path(lnk_path)

    def create(self, exe, lnk_path, icon=None, **kwargs):
        lnk = Path(lnk_path)
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.write_bytes(b"fake lnk")
        self.created.append((Path(exe), lnk, icon))
        return lnk

    def sync_targeting(self, exe, icon=None, roots=None):
        return []


class _Cache:
    def __call__(self, paths=None, **kwargs):
        return RefreshResult(True, True, len(paths or []))


def _replacer(editor=None, shortcuts=None):
    return BatchIconReplacer(
        inspector=_Inspector(),
        editor=editor or _Editor(),
        shortcuts=shortcuts or _Shortcuts(),
        cache=_Cache(),
    )


# --------------------------------------------------------------------------- #
# 功能一：只处理 exe
# --------------------------------------------------------------------------- #
def test_scan_mode_exe_ignores_shortcuts(workdir):
    _touch(workdir / "a.exe")
    _touch(workdir / "b.lnk")

    planned = _replacer().scan(workdir, mode="exe")

    assert [item.kind for item in planned] == ["exe"]


def test_locked_exe_degrades_to_shortcut_instead_of_skipping(workdir):
    _touch(workdir / "busy.exe")
    shortcuts = _Shortcuts()
    summary = _replacer(_Editor(locked=["busy.exe"]), shortcuts).run(
        workdir, _ico(workdir), mode="exe", shortcut_dir=workdir  # 别写到真实桌面
    )

    assert summary.replaced == 0
    assert summary.shortcuts_created == 1
    assert summary.results[0].action == ACTION_SHORTCUT
    assert shortcuts.created[0][1].name == "busy - 快捷方式.lnk"
    assert shortcuts.created[0][1].parent == workdir
    assert "正在运行" in summary.results[0].message


# --------------------------------------------------------------------------- #
# 功能二：只改选中的快捷方式
# --------------------------------------------------------------------------- #
def test_plan_paths_keeps_every_selected_file(workdir):
    """统一入口：选了什么就产生什么条目；不支持的也保留为“跳过”，不静默消失。"""
    lnk_a = _touch(workdir / "a.lnk", b"lnk")
    lnk_b = _touch(workdir / "sub" / "b.lnk", b"lnk")
    exe = _touch(workdir / "c.exe")
    other = _touch(workdir / "note.txt", b"x")
    missing = workdir / "missing.lnk"

    planned = _replacer().plan_paths([lnk_a, lnk_b, exe, other, missing])

    assert [item.path for item in planned] == [lnk_a, exe, missing, other, lnk_b]
    kinds = {item.path.name: item.kind for item in planned}
    assert kinds["a.lnk"] == "lnk"
    assert kinds["c.exe"] == "exe"
    assert kinds["note.txt"] == "other"
    assert kinds["missing.lnk"] == "other"
    assert "不是 .exe / .lnk" in next(i for i in planned if i.path == other).note
    assert "不存在" in next(i for i in planned if i.path == missing).note


def test_unsupported_files_are_skipped_not_fatal(workdir):
    """无效文件只能记一行跳过，绝不能让整批任务崩掉。"""
    from iconswap.icon_replacer import ACTION_SKIPPED

    other = _touch(workdir / "note.txt", b"x")
    missing = workdir / "nope.lnk"
    replacer = _replacer()
    ico = _ico(workdir)

    summary = replacer.run(None, ico, planned=replacer.plan_paths([other, missing]))

    assert summary.failed == 0
    assert [r.action for r in summary.results] == [ACTION_SKIPPED, ACTION_SKIPPED]
    assert summary.results[1].as_log_line().startswith("[跳过]")


def test_run_with_planned_creates_no_shortcuts(workdir):
    lnk = _touch(workdir / "no-target.lnk", b"lnk")
    shortcuts = _Shortcuts()
    replacer = _replacer(shortcuts=shortcuts)
    ico = _ico(workdir)

    planned = replacer.plan_paths([lnk])
    assert planned[0].target is None and planned[0].note == ""
    summary = replacer.run(None, ico, planned=planned, mode="lnk")

    assert summary.results[0].action == ACTION_SHORTCUT_UPDATED
    assert shortcuts.icon_updates == [(lnk, ico)]
    assert shortcuts.created == []
    assert summary.shortcuts_created == 0


def test_mode_lnk_scan_keeps_empty_target_items(workdir):
    _touch(workdir / "a.exe")
    lnk = _touch(workdir / "a.lnk", b"lnk")

    planned = _replacer().scan(workdir, mode="lnk")

    assert [item.path for item in planned] == [lnk]
    assert planned[0].note == ""


# --------------------------------------------------------------------------- #
# 真实 COM：空目标 .lnk 端到端
# --------------------------------------------------------------------------- #
def test_real_empty_target_shortcut_gets_icon(workdir):
    pytest.importorskip("win32com.client")
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    lnk = workdir / "DeepSeek Harness.lnk"
    shortcut = shell.CreateShortcut(str(lnk))
    shortcut.Save()
    assert shell.CreateShortcut(str(lnk)).TargetPath == ""

    ico = _ico(workdir)
    before = {p.name for p in workdir.glob("*.lnk")}
    replacer = BatchIconReplacer()
    summary = replacer.run(workdir, ico, mode="lnk")

    assert summary.results[0].action == ACTION_SHORTCUT_UPDATED
    icon = str(shell.CreateShortcut(str(lnk)).IconLocation or "")
    assert icon_parts(icon) == icon_parts(icon_location_for(ico))
    assert {p.name for p in workdir.glob("*.lnk")} == before  # 没有新建
    assert shell.CreateShortcut(str(lnk)).TargetPath == ""    # 目标没被瞎改


# --------------------------------------------------------------------------- #
# HTTP 接口：files 清单与 mode=exe
# --------------------------------------------------------------------------- #
def _wait_job(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/batch/jobs/{job_id}").get_json()
        if payload.get("state") != "running":
            return payload
        time.sleep(0.05)
    return payload


def test_start_api_accepts_explicit_file_list(workdir):
    from iconswap.app import create_app

    client = create_app().test_client()
    lnk = _touch(workdir / "picked.lnk", b"lnk")
    other = _touch(workdir / "ignored.lnk", b"lnk")
    ico = _ico(workdir)

    response = client.post(
        "/api/batch/start",
        json={
            "files": [str(lnk)],
            "ico_path": str(ico),
            "mode": "lnk",
        },
    )

    assert response.status_code == 202, response.get_json()
    payload = response.get_json()
    assert payload["files"] == 1

    done = _wait_job(client, payload["job_id"])
    assert done["state"] == "done", done
    assert done["result"]["shortcuts_updated"] == 1, done["logs"]
    assert done["result"]["shortcuts_created"] == 0

    icon_lnk = json.loads(json.dumps(done["result"]["results"]))
    assert icon_lnk[0]["name"] == "picked.lnk"
    assert icon_lnk[0]["name"] != "ignored.lnk"


def test_start_api_rejects_missing_ico(workdir):
    from iconswap.app import create_app

    client = create_app().test_client()
    response = client.post(
        "/api/batch/start",
        json={"files": [str(workdir / "a.lnk")], "mode": "lnk"},
    )
    assert response.status_code == 400
    assert "ico" in response.get_json()["error"].lower()


def test_scan_api_supports_exe_mode(workdir):
    from iconswap.app import create_app

    _touch(workdir / "a.exe")
    _touch(workdir / "a.lnk", b"lnk")
    client = create_app().test_client()

    response = client.post(
        "/api/batch/scan", json={"folder": str(workdir), "mode": "exe"}
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["mode"] == "exe"
    assert payload["exe_count"] == 1
    assert payload["lnk_count"] == 0


def test_start_api_rejects_unknown_mode(workdir):
    from iconswap.app import create_app

    client = create_app().test_client()
    response = client.post(
        "/api/batch/start",
        json={"folder": str(workdir), "mode": "bogus", "ico_path": "x.ico"},
    )
    assert response.status_code == 400
