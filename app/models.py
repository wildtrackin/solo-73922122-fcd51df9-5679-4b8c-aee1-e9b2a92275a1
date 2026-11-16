"""Pydantic 响应模型与任务状态枚举。"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务生命周期状态。

    QUEUED      已受理，等待重算名额
    RUNNING     正在重算
    COMPLETED   正常完成，结果已发布
    FAILED      整批失败（公式错误/重算失败/超时），不发布逐样本结果
    CANCELLED   已取消，不发布结果
    INTERRUPTED 服务重启时发现该批未走完终态
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


# 终态集合：进入后不可再取消
TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.INTERRUPTED,
}

# 不发布逐样本结果的终态
NO_RESULT_STATUSES = {
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.INTERRUPTED,
}


class SampleResult(BaseModel):
    """单个样本的核对结果。"""

    row: int = Field(..., description="样本在样本表中的行号（从 1 开始，含表头）")
    sample_id: str = Field(..., description="A 列唯一样本号")
    cached_verdict: Optional[str] = Field(
        None, description="工作簿内原缓存判定：PASS / FAIL；缺缓存为 unknown"
    )
    recalculated_verdict: str = Field(
        ..., description="LibreOffice 实际重算判定：PASS / FAIL"
    )
    changed: bool = Field(..., description="缓存与重算是否不一致（缓存缺失视为变化）")
    change_type: str = Field(
        ...,
        description="unchanged / pass_to_fail / fail_to_pass / cache_unknown",
    )


class TaskRecord(BaseModel):
    """任务持久化/查询记录。"""

    task_id: str
    status: TaskStatus
    filename: str
    size_bytes: int
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    cancel_requested: bool = False
    samples: Optional[list[SampleResult]] = None
    summary: Optional[dict] = None


class CreateTaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    filename: str
    size_bytes: int
    message: str


class CancelResponse(BaseModel):
    task_id: str
    status: TaskStatus
    cancel_requested: bool
    message: str


class ErrorResponse(BaseModel):
    error_code: str
    message: str
