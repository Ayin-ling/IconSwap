"""回归测试：UI 遮挡/弹窗状态机、运行时临时目录兜底。

这些用例专门盯住两个曾经真实发生的问题：
1. `.modal { display: grid }` 覆盖了 `[hidden]`，导致遮罩层永远盖住页面，
   表现成"按钮点了没反应、界面卡死"；
2. 入口处创建临时目录时，非管理员账户会 PermissionError 导致启动即崩。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from iconswap import app as app_module  # noqa: E402


# --------------------------------------------------------------------------- #
# CSS / 标记层
# --------------------------------------------------------------------------- #
def test_hidden_attribute_override_rule_exists():
    """必须存在 `[hidden] { display: none !important; }` 兜底规则。"""
    import re

    css = (ROOT / "src" / "iconswap" / "static" / "css" / "style.css").read_text(encoding="utf-8")

    pattern = re.compile(r"\[hidden\]\s*\{[^}]*display:\s*none[^}]*!important", re.S)
    assert pattern.search(css), "缺少 [hidden]{display:none !important} 兜底规则"


def test_modal_starts_hidden_in_markup():
    """弹窗初始必须是 hidden，否则会在页面加载时盖住整个界面。"""
    html = (ROOT / "src" / "iconswap" / "templates" / "index.html").read_text(encoding="utf-8")

    marker = html.index('id="about-modal"')
    tag = html[max(0, marker - 200): marker + 200]
    assert "hidden" in tag, "about-modal 必须带 hidden 属性"


def test_signed_modal_is_gone():
    """签名确认弹窗已按需求移除：签名程序直接走“建快捷方式 + 换图标”。"""
    html = (ROOT / "src" / "iconswap" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "src" / "iconswap" / "static" / "js" / "batch.js").read_text(encoding="utf-8")

    assert "signed-modal" not in html
    assert "signed-yes" not in html
    assert "askSignedPolicy" not in js


def test_modal_overlay_is_fixed_and_covers_page():
    """遮罩层是全屏 fixed，一旦被误显示就会挡住所有点击 —— 记下这个事实。"""
    css = (ROOT / "src" / "iconswap" / "static" / "css" / "style.css").read_text(encoding="utf-8")

    block = css.split(".modal {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in block
    assert "inset: 0" in block


# --------------------------------------------------------------------------- #
# 前端：EXE / LNK 已合并为唯一的“一键智能替换”面板
# --------------------------------------------------------------------------- #
def test_batch_js_is_single_smart_panel():
    js = (ROOT / "src" / "iconswap" / "static" / "js" / "batch.js").read_text(encoding="utf-8")

    # 整页只有一个 Panel，旧的“双面板”实现已删除
    assert js.count("new Panel(") == 1
    assert "setupExeFeature" not in js
    assert "setupLnkFeature" not in js
    # 任务进行中忽略重复点击
    assert "if (panel.busy)" in js
    # 一份混合清单交给后台，由后端按后缀分流
    assert "files: picked" in js
    assert 'mode: "all"' in js
    assert "ico_path: icon" in js
    # 图标只走真实路径，不再上传文件
    assert "FormData" not in js
    # 三种日志前缀由后端给出
    assert "[失败]" in js


def test_index_has_single_smart_replace_area():
    html = (ROOT / "src" / "iconswap" / "templates" / "index.html").read_text(encoding="utf-8")

    # 唯一的执行入口
    assert "🚀 一键智能替换" in html
    assert 'id="smart-start"' in html
    # 全局图标源 + 两种目标选择
    assert 'id="smart-ico-path"' in html
    assert 'id="smart-ico-pick"' in html
    assert 'id="smart-folder"' in html
    assert 'id="smart-files"' in html
    assert 'id="smart-recursive"' in html
    assert 'id="smart-log"' in html
    # 旧的两个模块与按钮已彻底移除
    for legacy in ('id="exe-start"', 'id="lnk-start"', 'id="exe-ico-path"',
                   'id="lnk-ico-path"', 'id="lnk-pick"', "一键替换 EXE (自动降级)",
                   "仅替换选中的快捷方式"):
        assert legacy not in html, legacy


# --------------------------------------------------------------------------- #
# 临时目录兜底
# --------------------------------------------------------------------------- #
def test_runtime_temp_bootstrap_is_consistent():
    """返回值要么是 None，要么是一个真实可写的目录，且 TEMP/TMP 与之同步。"""
    result = app_module._bootstrap_runtime_temp()

    if result is None:
        return  # 受限环境下允许失败

    path = Path(result)
    assert path.is_dir()
    probe = path / ".iconswap_unit_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    assert app_module.RUNTIME_TEMP_DIR == result


def test_runtime_temp_bootstrap_never_raises(monkeypatch):
    """创建目录失败（权限不足）时必须静默返回 None，绝不能让启动崩溃。"""

    def boom(*args, **kwargs):
        raise PermissionError("模拟非管理员账户无法在 C:\\ 建目录")

    monkeypatch.setattr(app_module._os_boot, "makedirs", boom)

    assert app_module._bootstrap_runtime_temp() is None


def test_app_import_survives_missing_temp_dir():
    """模块级兜底变量必须已定义，且不会因为失败而缺失。"""
    assert hasattr(app_module, "RUNTIME_TEMP_DIR")
    assert app_module.RUNTIME_TEMP_DIR is None or isinstance(app_module.RUNTIME_TEMP_DIR, str)

# --------------------------------------------------------------------------- #
# 日志落盘位置（需求一：禁止在程序根目录生成日志/文本文件）
# --------------------------------------------------------------------------- #
def test_log_path_is_never_in_program_dir():
    import ui_window as dl

    resolved = dl._resolve_log_path()
    if resolved is None:
        return  # 全部不可写时允许没有文件日志

    assert resolved.name == dl.LOG_FILENAME
    program_dir = Path(sys.executable).parent.resolve()
    assert resolved.parent.resolve() != program_dir
    assert resolved.parent.resolve() != Path.cwd().resolve()


def test_setup_logging_does_not_create_files_in_cwd():
    """调用日志初始化后，当前工作目录不能多出任何文件。"""
    import logging

    import ui_window as dl

    before = {item.name for item in Path.cwd().iterdir()}
    root_logger = logging.getLogger()
    saved_level = root_logger.level
    saved_handlers = list(root_logger.handlers)

    try:
        dl._setup_logging()
        root_logger.warning("unit-test")
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in saved_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(saved_level)

    after = {item.name for item in Path.cwd().iterdir()}
    created = after - before - {"__pycache__"}
    assert not created, f"日志初始化在工作目录创建了文件：{created}"
