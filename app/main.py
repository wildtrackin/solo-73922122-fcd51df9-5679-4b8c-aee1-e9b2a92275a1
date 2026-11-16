"""FastAPI 入口：上传核对、按任务号查询/取消、状态总览。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from . import config
from .manager import TaskManager
from .models import (
    CancelResponse,
    CreateTaskResponse,
    TaskRecord,
    TaskStatus,
)
from .security import WorkbookRejected

manager = TaskManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    config.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    # 重启恢复：未走完终态的批次标记为中断
    manager.recover_on_startup()
    yield


app = FastAPI(
    title="检测工作簿 PASS/FAIL 缓存核对 API",
    description=(
        "上传 xlsx 检测工作簿，用 LibreOffice 实际重算副本，"
        "逐样本比对原缓存判定与重算判定，识别缓存失真（PASS→FAIL 等）。"
        "原件不修改；拒收宏与外链；每批重算限 10 秒，最多两批并行。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(WorkbookRejected)
async def rejected_handler(_request, exc: WorkbookRejected):
    return JSONResponse(
        status_code=422,
        content={"error_code": exc.error_code, "message": exc.message},
    )


@app.get("/health", tags=["运维"])
async def health():
    return {"status": "ok", "slots_in_use": manager.slots.in_use,
            "slots_capacity": config.MAX_CONCURRENT_BATCHES}


@app.post(
    "/tasks",
    response_model=CreateTaskResponse,
    status_code=202,
    tags=["核对任务"],
    summary="上传工作簿并创建核对任务",
)
async def create_task(file: UploadFile = File(..., description="≤2 MiB 的 xlsx")):
    filename = file.filename or "uploaded.xlsx"
    lower_name = filename.lower()
    # 非 Office 表格扩展名直接拒绝；xlsm/xltm/xlam 等宏格式放行到安全扫描，
    # 由 scan_container 给出 422 MACRO_NOT_ALLOWED
    office_suffixes = (".xlsx", ".xlsm", ".xltm", ".xlam", ".xls", ".xlsb")
    if not lower_name.endswith(office_suffixes):
        raise HTTPException(
            status_code=400,
            detail={"error_code": "BAD_EXTENSION",
                    "message": "仅支持 Excel 工作簿（.xlsx，拒收宏工作簿）"},
        )
    content_type = (file.content_type or "").lower()
    if content_type and content_type not in config.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "BAD_CONTENT_TYPE",
                    "message": f"不支持的内容类型：{content_type}"},
        )

    # 读取并在应用层强制 2 MiB 上限（多读出 1 字节即可判定超限）
    content = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error_code": "FILE_TOO_LARGE",
                "message": f"工作簿超过 {config.MAX_UPLOAD_BYTES} 字节（2 MiB）上限",
            },
        )
    if not content:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "EMPTY_FILE", "message": "上传文件为空"},
        )

    record = await manager.create_task(content, filename)
    return CreateTaskResponse(
        task_id=record["task_id"],
        status=TaskStatus(record["status"]),
        filename=record["filename"],
        size_bytes=record["size_bytes"],
        message="任务已受理并进入重算队列",
    )


@app.get(
    "/tasks/{task_id}",
    response_model=TaskRecord,
    tags=["核对任务"],
    summary="按任务号查询状态与核对结果",
)
async def get_task(task_id: str):
    record = await manager.get(task_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "TASK_NOT_FOUND",
                    "message": f"任务不存在：{task_id}"},
        )
    return TaskRecord(**record)


@app.post(
    "/tasks/{task_id}/cancel",
    response_model=CancelResponse,
    tags=["核对任务"],
    summary="取消任务（立即受理；已完成不可取消）",
)
async def cancel_task(task_id: str):
    try:
        record, accepted = await manager.request_cancel(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "TASK_NOT_FOUND",
                    "message": f"任务不存在：{task_id}"},
        )
    if not accepted:
        return CancelResponse(
            task_id=task_id,
            status=TaskStatus(record["status"]),
            cancel_requested=True,
            message=f"任务已处于终态 {record['status']}，不可取消",
        )
    return CancelResponse(
        task_id=task_id,
        status=TaskStatus(record["status"]),
        cancel_requested=True,
        message=(
            "取消已立即受理，进程退出且暂存清理后释放名额"
            if record["status"] == TaskStatus.RUNNING.value
            else "排队任务已取消，结果不发布"
        ),
    )


@app.get("/tasks", tags=["核对任务"], summary="列出全部任务（状态总览）")
async def list_tasks():
    records = await manager.list_all()
    return {"count": len(records), "tasks": records}
