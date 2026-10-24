"""批量替换任务的后台执行与进度查询。

Web UI 不能同步等几十秒，所以：
``/api/batch/start`` 立刻返回 ``job_id`` → 后台线程跑
:class:`iconswap.batch_replacer.BatchIconReplacer` → 前端轮询
``/api/batch/jobs/<id>`` 拿日志与结果。

任务状态保存在进程内存里（最多保留 20 个），重启即清空。
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from iconswap.icon_replacer import BatchIconReplacer, BatchSummary, FileResult
from iconswap.icon_replacer import POLICY_SHORTCUT, POLICIES

__all__ = ["Job", "JobRegistry"]

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"

_MAX_LOGS = 500


def _com_initialize() -> None:
    """后台线程必须自己初始化 COM（WScript.Shell 需要）。"""
    try:
        import pythoncom  # noqa: PLC0415 - 惰性导入
    except ImportError:  # pragma: no cover - 缺 pywin32
        return
    try:
        pythoncom.CoInitialize()
    except Exception:  # pragma: no cover
        pass


def _com_uninitialize() -> None:
    try:
        import pythoncom  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return
    try:
        pythoncom.CoUninitialize()
    except Exception:  # pragma: no cover
        pass


@dataclass
class Job:
    """一次批量替换任务。"""

    id: str
    folder: str
    policy: str
    created: float = field(default_factory=time.time)
    state: str = STATE_RUNNING
    total: int = 0
    done: int = 0
    logs: list[str] = field(default_factory=list)
    error: Optional[str] = None
    summary: Optional[BatchSummary] = None

    def log(self, message: str) -> None:
        self.logs.append(message)
        if len(self.logs) > _MAX_LOGS:
            del self.logs[: len(self.logs) - _MAX_LOGS]

    def to_dict(self) -> dict:
        payload = {
            "id": self.id,
            "state": self.state,
            "folder": self.folder,
            "policy": self.policy,
            "total": self.total,
            "done": self.done,
            "logs": self.logs[-_MAX_LOGS:],
            "error": self.error,
            "elapsed": round(time.time() - self.created, 1),
        }
        if self.summary is not None:
            payload["result"] = self.summary.to_dict()
        return payload


class JobRegistry:
    """线程安全的任务注册表。"""

    def __init__(self, max_jobs: int = 20) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    # -- 查询 -------------------------------------------------------------
    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def all_jobs(self) -> list[Job]:
        with self._lock:
            return [self._jobs[job_id] for job_id in self._order]

    # -- 创建 -------------------------------------------------------------
    def start(
        self,
        folder: "str | os.PathLike[str] | None",
        ico: "str | os.PathLike[str]",
        *,
        policy: str = POLICY_SHORTCUT,
        recursive: bool = True,
        include_lnk: bool = True,
        mode: str = "all",
        only_paths: "list[str] | None" = None,
        shortcut_dir: "str | os.PathLike[str] | None" = None,
        files: "list[str] | None" = None,
        replacer_factory: Callable[[], BatchIconReplacer] = BatchIconReplacer,
    ) -> Job:
        """创建并启动一个后台任务，立即返回 :class:`Job`。

        :param files: 显式文件清单（功能二“多选 .lnk 文件”）；给出时不再扫描
            ``folder``，只处理清单里的文件。
        """
        if policy not in POLICIES:
            raise ValueError(f"policy 必须是 {POLICIES} 之一")

        display_folder = str(folder or "")
        if not display_folder and files:
            try:
                display_folder = str(Path(files[0]).parent)
            except (TypeError, ValueError, IndexError):  # pragma: no cover
                display_folder = ""

        job = Job(id=uuid.uuid4().hex[:12], folder=display_folder, policy=policy)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._max_jobs:
                oldest = self._order.pop(0)
                self._jobs.pop(oldest, None)

        thread = threading.Thread(
            target=self._run,
            args=(job, folder, ico, policy, recursive, include_lnk, mode, only_paths,
                  shortcut_dir, files, replacer_factory),
            name=f"iconswap-batch-{job.id}",
            daemon=True,
        )
        thread.start()
        return job

    # -- 执行 -------------------------------------------------------------
    def _run(
        self,
        job: Job,
        folder: "str | os.PathLike[str] | None",
        ico: "str | os.PathLike[str]",
        policy: str,
        recursive: bool,
        include_lnk: bool,
        mode: str,
        only_paths: Optional[list[str]],
        shortcut_dir: Optional[str],
        files: Optional[list[str]],
        replacer_factory: Callable[[], BatchIconReplacer],
    ) -> None:
        """后台线程入口：自己初始化 COM，跑完再反初始化。"""
        _com_initialize()
        try:
            self._run_job(
                job,
                folder,
                ico,
                policy,
                recursive,
                include_lnk,
                mode,
                only_paths,
                shortcut_dir,
                files,
                replacer_factory,
            )
        finally:
            _com_uninitialize()

    def _run_job(
        self,
        job: Job,
        folder: "str | os.PathLike[str] | None",
        ico: "str | os.PathLike[str]",
        policy: str,
        recursive: bool,
        include_lnk: bool,
        mode: str,
        only_paths: Optional[list[str]],
        shortcut_dir: Optional[str],
        files: Optional[list[str]],
        replacer_factory: Callable[[], BatchIconReplacer],
    ) -> None:
        job.log(f"开始处理：{job.folder or '（按文件清单）'}")
        job.log(f"图标文件：{ico}")
        if files:
            job.log(f"文件清单：共 {len(files)} 个目标，只处理这些文件")

        def on_progress(index: int, total: int, result: FileResult) -> None:
            job.total = total
            job.done = index
            job.log(result.as_log_line())

        try:
            replacer = replacer_factory()
            planned = replacer.plan_paths(files) if files else None
            summary = replacer.run(
                None if planned is not None else folder,
                ico,
                policy=policy,
                recursive=recursive,
                include_lnk=include_lnk,
                mode=mode,
                only_paths=only_paths,
                progress=on_progress,
                shortcut_dir=shortcut_dir,
                planned=planned,
            )
            job.summary = summary
            if summary.aborted:
                job.state = STATE_ERROR
                job.error = "缺少 rcedit.exe，替换在中途停止"
                job.log(job.error)
            else:
                job.state = STATE_DONE
            job.log(summary.as_text())
        except Exception as exc:  # noqa: BLE001 - 任何异常都要反映到任务状态
            job.state = STATE_ERROR
            job.error = f"{type(exc).__name__}: {exc}"
            job.log(job.error)
            job.log(traceback.format_exc(limit=3))
