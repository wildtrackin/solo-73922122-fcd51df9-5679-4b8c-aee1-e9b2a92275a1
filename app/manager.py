"""任务编排：名额控制、状态机、持久化与重启恢复。

并发模型：
- asyncio 单事件循环；MAX_CONCURRENT_BATCHES 个重算名额，用 Condition 管理；
- 每个任务一个 asyncio.Task 协程；取消用 asyncio.Event 通知；
- 取消/超时后必须等计算进程及子进程退出、暂存清理完毕，才释放名额并落终态；
- 另一个批次与状态查询不受影响（状态查询只读锁保护的字典）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import NO_RESULT_STATUSES, TERMINAL_STATUSES, TaskStatus
from .recalc import (
    RecalcCancelled,
    RecalcFailed,
    RecalcTimeout,
    cleanup_task_dir,
    run_recalc,
)
from .security import WorkbookRejected, inspect_workbook, scan_container
from .workbook_io import (
    UnexpectedVerdict,
    WorkbookFormulaError,
    read_cached_verdicts,
    read_recalculated_verdicts,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SlotManager:
    """固定数量的重算名额，支持排队等待。"""

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._in_use = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            while self._in_use >= self._capacity:
                await self._cond.wait()
            self._in_use += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_use = max(0, self._in_use - 1)
            self._cond.notify_all()

    @property
    def in_use(self) -> int:
        return self._in_use


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, dict] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self.slots = SlotManager(config.MAX_CONCURRENT_BATCHES)

    # ---------- 持久化 ----------

    def _task_file(self, task_id: str) -> Path:
        return config.TASKS_DIR / f"{task_id}.json"

    def _persist(self, record: dict) -> None:
        config.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._task_file(record["task_id"]).with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self._task_file(record["task_id"]))

    async def _update(self, task_id: str, **fields) -> dict:
        async with self._lock:
            record = self._tasks[task_id]
            record.update(fields)
            self._persist(record)
            return dict(record)

    async def get(self, task_id: str) -> dict | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            return dict(record) if record else None

    async def list_all(self) -> list[dict]:
        async with self._lock:
            return [dict(r) for r in self._tasks.values()]

    # ---------- 重启恢复 ----------

    def recover_on_startup(self) -> None:
        """重启后把未走完终态的任务标记为 interrupted，并清理暂存。"""
        config.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        for path in sorted(config.TASKS_DIR.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            status = record.get("status")
            if status not in {s.value for s in TERMINAL_STATUSES}:
                record["status"] = TaskStatus.INTERRUPTED.value
                record["finished_at"] = _now_iso()
                record["error_code"] = "INTERRUPTED_BY_RESTART"
                record["error_message"] = "服务重启，该批次未完成，标记为中断"
                record["samples"] = None
                record["summary"] = None
                self._persist(record)
            self._tasks[record["task_id"]] = record
        # 重启后所有暂存目录都已无主，统一清掉
        for child in config.STAGING_DIR.glob("*"):
            if child.is_dir():
                cleanup_task_dir(child)

    # ---------- 创建 ----------

    async def create_task(self, content: bytes, filename: str) -> dict:
        """容器与结构校验通过后落盘原件并启动后台协程。"""
        scan_container(content, filename)  # 拒收抛 WorkbookRejected
        inspect = inspect_workbook(content)  # 同上
        rows_info = inspect["rows"]

        task_id = uuid.uuid4().hex
        task_dir = config.STAGING_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "input.xlsx").write_bytes(content)  # 原件副本，只读使用

        record = {
            "task_id": task_id,
            "status": TaskStatus.QUEUED.value,
            "filename": filename,
            "size_bytes": len(content),
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "error_code": None,
            "error_message": None,
            "cancel_requested": False,
            "samples": None,
            "summary": None,
        }
        async with self._lock:
            self._tasks[task_id] = record
            self._cancel_events[task_id] = asyncio.Event()
            self._persist(record)

        job = asyncio.create_task(
            self._run(task_id, task_dir, rows_info), name=f"recalc-{task_id}"
        )
        self._jobs[task_id] = job
        job.add_done_callback(lambda _j: self._jobs.pop(task_id, None))
        return dict(record)

    # ---------- 取消 ----------

    async def request_cancel(self, task_id: str) -> tuple[dict, bool]:
        """返回 (记录, 是否受理成功)。已完成等终态不可取消。

        QUEUED 任务没有计算进程，立即落 CANCELLED 终态并撤销排队协程；
        RUNNING 任务立即受理（cancel_requested=True 并置事件），
        待进程组退出、暂存清理后由工作协程落终态。
        """
        job_to_cancel: asyncio.Task | None = None
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if TaskStatus(record["status"]) in TERMINAL_STATUSES:
                return dict(record), False
            record["cancel_requested"] = True
            event = self._cancel_events.get(task_id)
            if event is not None:
                event.set()
            if record["status"] == TaskStatus.QUEUED.value:
                record["status"] = TaskStatus.CANCELLED.value
                record["finished_at"] = _now_iso()
                record["error_code"] = "CANCELLED"
                record["error_message"] = "排队期间取消，结果不发布"
                self._persist(record)
                job_to_cancel = self._jobs.get(task_id)
                snapshot = dict(record)
            else:
                self._persist(record)
                snapshot = dict(record)
        if job_to_cancel is not None:
            job_to_cancel.cancel()
            cleanup_task_dir(config.STAGING_DIR / task_id)
        return snapshot, True

    # ---------- 主流程 ----------

    async def _run(
        self, task_id: str, task_dir: Path, rows_info: list[tuple]
    ) -> None:
        event = self._cancel_events[task_id]
        # 排队等待名额；拿到名额后，所有终态处理（含进程退出确认与暂存清理）
        # 都在释放名额之前完成
        try:
            await self.slots.acquire()
        except asyncio.CancelledError:
            # QUEUED 期间被取消：request_cancel 已落 CANCELLED 终态并清暂存
            raise
        try:
            try:
                if event.is_set():
                    await self._finish_cancelled(task_id, task_dir)
                    return
                await self._update(task_id, status=TaskStatus.RUNNING.value,
                                   started_at=_now_iso())

                # 先读原件缓存（原件不做任何修改）
                content = (task_dir / "input.xlsx").read_bytes()
                cached = read_cached_verdicts(content, rows_info)

                out_file = await run_recalc(task_dir, cancel_event=event)
                recalculated = read_recalculated_verdicts(
                    str(out_file), rows_info
                )

                if event.is_set():
                    await self._finish_cancelled(task_id, task_dir)
                    return

                await self._publish_results(
                    task_id, task_dir, rows_info, cached, recalculated
                )
            except WorkbookFormulaError as exc:
                await self._finish_failed(
                    task_id, task_dir, "FORMULA_ERROR", str(exc)
                )
            except RecalcTimeout as exc:
                await self._finish_failed(
                    task_id, task_dir, "RECALC_TIMEOUT", str(exc)
                )
            except RecalcCancelled:
                await self._finish_cancelled(task_id, task_dir)
            except RecalcFailed as exc:
                await self._finish_failed(
                    task_id, task_dir, "RECALC_FAILED", str(exc)
                )
            except UnexpectedVerdict as exc:
                await self._finish_failed(
                    task_id, task_dir, "UNEXPECTED_VERDICT", str(exc)
                )
            except Exception as exc:  # 防御性兜底，保证任务必有终态
                import sys
                import traceback
                print(
                    f"[manager] 任务 {task_id} 未预期异常：{exc!r}\n"
                    + "".join(traceback.format_exc()),
                    file=sys.stderr, flush=True,
                )
                await self._finish_failed(
                    task_id, task_dir, "INTERNAL_ERROR", repr(exc)[:300]
                )
        finally:
            # 至此计算进程（含子进程）已退出、暂存已清完，方可释放名额
            await self.slots.release()

    async def _publish_results(
        self, task_id, task_dir, rows_info, cached, recalculated
    ) -> None:
        samples = []
        counts = {
            "total": len(rows_info),
            "unchanged": 0,
            "changed": 0,
            "pass_to_fail": 0,
            "fail_to_pass": 0,
            "cache_unknown": 0,
        }
        for row_idx, sample_id, _cell in rows_info:
            c = cached.get(row_idx)
            r = recalculated[row_idx]
            if c is None:
                change_type = "cache_unknown"
                changed = True
            elif c == r:
                change_type = "unchanged"
                changed = False
            elif c == "PASS" and r == "FAIL":
                change_type = "pass_to_fail"
                changed = True
            elif c == "FAIL" and r == "PASS":
                change_type = "fail_to_pass"
                changed = True
            else:
                change_type = "unchanged"
                changed = False
            counts[change_type] += 1
            samples.append(
                {
                    "row": row_idx,
                    "sample_id": sample_id,
                    "cached_verdict": c,
                    "recalculated_verdict": r,
                    "changed": changed,
                    "change_type": change_type,
                }
            )

        summary = {
            "total": len(rows_info),
            "unchanged": counts["unchanged"],
            "changed": (
                counts["pass_to_fail"]
                + counts["fail_to_pass"]
                + counts["cache_unknown"]
            ),
            "pass_to_fail": counts["pass_to_fail"],
            "fail_to_pass": counts["fail_to_pass"],
            "cache_unknown": counts["cache_unknown"],
            "drift_pass_to_fail": counts["pass_to_fail"],
        }
        # 先发布（持久化）结果，再清暂存、落终态
        await self._update(
            task_id,
            status=TaskStatus.COMPLETED.value,
            finished_at=_now_iso(),
            samples=samples,
            summary=summary,
        )
        cleanup_task_dir(task_dir)

    async def _finish_failed(
        self, task_id: str, task_dir: Path, code: str, message: str
    ) -> None:
        # 此时进程已退出（超时/失败路径均已 kill/wait）；清理暂存后落终态
        cleanup_task_dir(task_dir)
        await self._update(
            task_id,
            status=TaskStatus.FAILED.value,
            finished_at=_now_iso(),
            error_code=code,
            error_message=message,
            samples=None,
            summary=None,
        )

    async def _finish_cancelled(self, task_id: str, task_dir: Path) -> None:
        # 进程已退出且暂存清完，才落 CANCELLED 终态
        cleanup_task_dir(task_dir)
        await self._update(
            task_id,
            status=TaskStatus.CANCELLED.value,
            finished_at=_now_iso(),
            error_code="CANCELLED",
            error_message="任务已取消，结果不发布",
            samples=None,
            summary=None,
        )
