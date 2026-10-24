"""批量替换相关的 HTTP 接口（Flask 蓝图）。

接口
----
====================================  ==================================
方法 / 路径                            说明
====================================  ==================================
``POST /api/batch/scan``              扫描目标文件夹（exe / lnk / 两者）
``POST /api/batch/start``             启动后台任务（只处理勾选项，或直接
                                      给一份文件清单）
``GET  /api/batch/jobs/<job_id>``     查询任务进度与结果
``GET  /api/batch/tools``             检查 rcedit / pywin32 是否可用
====================================  ==================================

两套功能靠 ``mode`` 区分：
``exe`` = 功能一（改 exe，失败自动降级为桌面快捷方式）；
``lnk`` = 功能二（只改选中的快捷方式，绝不新建任何快捷方式）。

请求均为 JSON（也兼容 multipart 表单）；错误统一返回 ``{"error": "..."}`` 与 4xx/5xx。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify, request

from iconswap.batch_jobs import JobRegistry
from iconswap.icon_replacer import (
    MODE_ALL,
    MODE_EXE,
    MODE_LNK,
    POLICY_SHORTCUT,
    POLICY_SKIP,
    POLICIES,
    BatchIconReplacer,
)
from iconswap.resource_tools import ResourceToolMissing, locate_rcedit
from iconswap.shortcut_tools import ensure_com

__all__ = ["bp", "registry"]

bp = Blueprint("batch", __name__)
registry = JobRegistry()

#: 单次扫描返回给前端的文件上限（避免超大目录把响应撑爆）
_MAX_SCAN_FILES = 500

#: 允许的扫描/执行模式
_MODES = (MODE_ALL, MODE_EXE, MODE_LNK)


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _payload() -> dict:
    """请求体：优先 JSON，兼容 multipart/form。"""
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body
    return request.form.to_dict()


def _parse_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _parse_selected(raw, field: str = "selected") -> "list[str] | None":
    """解析 UI 勾选的路径列表（JSON 数组字符串）；``None`` 表示不限制。"""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw if str(item).strip()]
        return items or None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        # 兼容逗号分隔
        parsed = [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, (list, tuple)):
        raise ValueError(f"{field} 必须是路径数组")
    return [str(item) for item in parsed if str(item).strip()] or None


def _resolve_ico() -> Path:
    """从请求里取图标：必须是**磁盘上真实存在的 .ico 绝对路径**。

    为什么不收上传文件：快捷方式只记录图标路径，如果指向程序退出就清理的临时
    文件，图标会变成白图标。所以批量替换一律使用用户在那台机器上真实选中的
    ``.ico``，不做任何拷贝、不落任何缓存目录。
    """
    payload = _payload()
    raw_path = str(payload.get("ico_path") or "").strip().strip('"')

    if not raw_path:
        raise ValueError("请选择一个 .ico 图标文件（提供 ico_path 绝对路径）")

    ico = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not ico.is_file():
        raise ValueError(f"图标文件不存在：{ico}")
    if ico.suffix.lower() != ".ico":
        raise ValueError(f"图标必须是 .ico 文件：{ico.name}")
    return ico.resolve()


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@bp.post("/api/batch/scan")
def scan():
    """扫描目标文件夹，返回 ``.exe``（含签名状态）与 ``.lnk`` 列表。"""
    payload = request.get_json(silent=True) or {}
    folder = (payload.get("folder") or "").strip()
    if not folder:
        return _error("请提供 folder", 400)

    target = Path(folder).expanduser()
    if not target.is_dir():
        return _error(f"文件夹不存在：{target}", 400)

    recursive = _parse_bool(payload.get("recursive"), True)
    include_lnk = _parse_bool(payload.get("include_lnk"), True)
    mode = (payload.get("mode") or MODE_ALL).strip().lower()
    if mode not in _MODES:
        return _error(f"mode 只能是 {' / '.join(_MODES)}", 400)

    replacer = BatchIconReplacer()
    try:
        planned = replacer.scan(
            target, recursive=recursive, include_lnk=include_lnk, mode=mode
        )
    except NotADirectoryError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return _error(f"扫描失败：{exc}", 500)

    truncated = len(planned) > _MAX_SCAN_FILES
    items = [
        {
            "path": str(item.path),
            "name": item.path.name,
            "kind": item.kind,
            "signed": item.signed,
            "signature": item.signature.label if item.signature else (item.note or ""),
            "target": str(item.target) if item.target else None,
            "note": item.note,
        }
        for item in planned[:_MAX_SCAN_FILES]
    ]
    exe_items = [item for item in planned if item.kind == "exe"]
    lnk_items = [item for item in planned if item.kind == "lnk"]
    return jsonify(
        {
            "folder": str(target),
            "recursive": recursive,
            "include_lnk": include_lnk,
            "mode": mode,
            "total": len(planned),
            "truncated": truncated,
            "signed": sum(1 for item in exe_items if item.signed),
            "unsigned": sum(1 for item in exe_items if not item.signed),
            "exe_count": len(exe_items),
            "lnk_count": len(lnk_items),
            "files": items,
        }
    )


@bp.post("/api/batch/start")
def start():
    """启动批量替换任务，返回 job_id。

    两种调用方式：

    * ``folder``（+ ``mode``/``selected``）：扫描文件夹后处理；
    * ``files``（JSON 路径数组）：直接处理这份清单（功能二“多选 .lnk 文件”
      走的路径，不要求这些文件在同一个文件夹里）。

    图标一律通过 ``ico_path`` 给出磁盘上的真实 ``.ico``（不接受上传，
    避免把快捷方式指向会被清理的临时文件）。
    """
    payload = _payload()

    policy = str(payload.get("policy") or POLICY_SHORTCUT).strip().lower()
    if policy not in POLICIES:
        return _error(f"policy 只能是 {POLICIES}", 400)

    recursive = _parse_bool(payload.get("recursive"), True)
    include_lnk = _parse_bool(payload.get("include_lnk"), True)
    mode = str(payload.get("mode") or MODE_ALL).strip().lower()
    if mode not in _MODES:
        return _error(f"mode 只能是 {' / '.join(_MODES)}", 400)

    try:
        files = _parse_selected(payload.get("files"), field="files")
    except ValueError as exc:
        return _error(str(exc), 400)

    folder = str(payload.get("folder") or "").strip()
    target: Optional[Path] = None
    if folder:
        target = Path(folder).expanduser()
        if not target.is_dir():
            return _error(f"文件夹不存在：{target}", 400)
    elif not files:
        return _error("请提供 folder，或给一份 files 文件清单", 400)

    only_paths = _parse_selected(payload.get("selected"))
    shortcut_dir = str(payload.get("shortcut_dir") or "").strip() or None

    try:
        ico = _resolve_ico()
    except ValueError as exc:
        return _error(str(exc), 400)

    # 图标路径与写入方式先自检一次，坏路径在启动阶段就报错，而不是静默失败
    try:
        ensure_com()
    except Exception:  # noqa: BLE001 - 缺 pywin32 时由任务线程给出明确错误
        pass

    try:
        job = registry.start(
            target,
            ico,
            policy=policy,
            recursive=recursive,
            include_lnk=include_lnk,
            mode=mode,
            only_paths=only_paths,
            shortcut_dir=shortcut_dir,
            files=files,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(f"启动任务失败：{exc}", 500)

    return jsonify(
        {
            "job_id": job.id,
            "ico": str(ico),
            "policy": policy,
            "include_lnk": include_lnk,
            "mode": mode,
            "files": len(files) if files is not None else None,
            "selected": len(only_paths) if only_paths is not None else None,
        }
    ), 202


@bp.get("/api/batch/jobs/<job_id>")
def job_status(job_id: str):
    """查询任务进度。"""
    job = registry.get(job_id)
    if job is None:
        return _error("任务不存在或已过期", 404)
    return jsonify(job.to_dict())


@bp.get("/api/batch/tools")
def tools():
    """检查依赖工具是否就绪（UI 用来给提示）。"""
    rcedit_path: Optional[str] = None
    rcedit_error: Optional[str] = None
    try:
        rcedit_path = str(locate_rcedit())
    except ResourceToolMissing as exc:
        rcedit_error = str(exc)

    try:
        import win32com.client  # noqa: F401,PLC0415

        shortcuts_ok = True
    except ImportError:
        shortcuts_ok = False

    return jsonify(
        {
            "rcedit": rcedit_path,
            "rcedit_ready": rcedit_path is not None,
            "rcedit_error": rcedit_error,
            "shortcuts_ready": shortcuts_ok,
            "policies": list(POLICIES),
            "default_policy": POLICY_SHORTCUT,
            "skip_policy": POLICY_SKIP,
        }
    )
